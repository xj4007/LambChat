"""DeepAgents sandbox backend backed by one restricted Docker container."""

from __future__ import annotations

import asyncio
import io
import os
import shlex
import tarfile
from collections.abc import Iterable
from tempfile import SpooledTemporaryFile
from typing import Any, cast

from deepagents.backends.protocol import ExecuteResponse, GrepMatch, GrepResult
from deepagents.backends.sandbox import BaseSandbox

from src.infra.async_utils import run_blocking_io
from src.infra.backend.protocol_compat import (
    ExtendedFileError,
    FileDownloadResponse,
    FileUploadResponse,
    file_download_response,
    file_upload_response,
)
from src.infra.logging import get_logger
from src.infra.sandbox._docker_adapter import DockerSandboxAdapter
from src.infra.sandbox_grep import build_grep_command, get_sandbox_grep_timeout, parse_grep_response
from src.kernel.config import settings

logger = get_logger(__name__)

SANDBOX_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
SANDBOX_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
SANDBOX_BATCH_FILES_LIMIT = 100


def _grep_result(parsed: list[GrepMatch] | str, max_count: int | None) -> GrepResult:
    if isinstance(parsed, str):
        return GrepResult(error=parsed)
    truncated = max_count is not None and len(parsed) > max_count
    return GrepResult(
        matches=parsed[:max_count] if max_count is not None else parsed,
        truncated=truncated,
    )


class DockerSandboxBackend(BaseSandbox):
    """Implement the DeepAgents sandbox protocol over Docker Engine exec APIs."""

    def __init__(
        self,
        container: Any,
        adapter: DockerSandboxAdapter,
        *,
        timeout: int,
        max_output_bytes: int,
        env_vars: dict[str, str] | None = None,
        work_dir: str = "/tmp/lambchat-workspace",
    ) -> None:
        self._container = container
        self._adapter = adapter
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self.env_vars = env_vars if env_vars is not None else {}
        self._work_dir = work_dir
        self.enable_capture_offload = True

    @property
    def id(self) -> str:
        return str(getattr(self._container, "id", self._container))

    @property
    def work_dir(self) -> str:
        return self._work_dir

    def _resolve_path(self, path: str) -> str:
        if path == "/":
            return self.work_dir
        if path.startswith("/"):
            return path
        return f"{self.work_dir.rstrip('/')}/{path}"

    def resolve_path(self, path: str) -> str:
        return self._resolve_path(path)

    def _effective_timeout(self, requested: int | None) -> int:
        return min(requested or self._timeout, self._timeout)

    def _exec_environment(self) -> dict[str, str]:
        environment = dict(self.env_vars)
        for transport_key in (
            "DOCKER_HOST",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_CONTEXT",
        ):
            environment.pop(transport_key, None)
        environment.update(
            {
                "HOME": "/tmp/lambchat-home",
                "PYTHONUSERBASE": "/tmp/lambchat-home/.local",
                "PIP_USER": "1",
                "PATH": "/tmp/lambchat-home/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LAMBCHAT_WORKSPACE": self.work_dir,
            }
        )
        return environment

    def _command_for_exec(self, command: str, effective_timeout: int) -> str:
        work_dir = shlex.quote(self.work_dir)
        # Session directories are created lazily; mkdir must run before cd so
        # the first manager _ensure_work_dir call cannot fail on a fresh path.
        # Docker's SDK workdir remains /tmp. GNU timeout is inside the image so
        # the command deadline also covers a process that ignores SDK wrappers.
        return (
            f"mkdir -p {work_dir} && cd {work_dir} && timeout --signal=TERM --kill-after=5 "
            f"{effective_timeout}s sh -lc {shlex.quote(command)}"
        )

    @staticmethod
    def _frame_parts(frame: Any) -> Iterable[tuple[bytes, bytes]]:
        if isinstance(frame, tuple) and len(frame) == 2:
            stdout, stderr = frame
            yield bytes(stdout or b""), bytes(stderr or b"")
        elif isinstance(frame, (bytes, bytearray, memoryview)):
            yield bytes(frame), b""
        elif frame is not None:
            yield str(frame).encode(), b""

    @staticmethod
    def _exec_id(created: Any) -> str:
        if isinstance(created, dict):
            created = created.get("Id") or created.get("id")
        if not isinstance(created, str) or not created:
            raise RuntimeError("Docker sandbox command did not return an exec ID")
        return created

    @staticmethod
    def _append_frame(output: bytearray, frame: bytes, *, limit: int) -> bool:
        if not frame:
            return False
        remaining = limit - len(output)
        if remaining <= 0:
            return True
        if len(frame) > remaining:
            output.extend(frame[:remaining])
            return True
        output.extend(frame)
        return False

    def _exec_sync(self, command: str, effective_timeout: int) -> ExecuteResponse:
        api = self._adapter.exec_client.api
        command_for_exec = self._command_for_exec(command, effective_timeout)
        created = api.exec_create(
            self.id,
            cmd=["sh", "-lc", command_for_exec],
            workdir="/tmp",
            environment=self._exec_environment(),
            stdout=True,
            stderr=True,
        )
        exec_id = self._exec_id(created)
        output = bytearray()
        truncated = False
        stream = api.exec_start(exec_id, stream=True, demux=True)
        for frame in stream:
            for stdout, stderr in self._frame_parts(frame):
                truncated = (
                    self._append_frame(
                        output,
                        stdout,
                        limit=self._max_output_bytes,
                    )
                    or truncated
                )
                truncated = (
                    self._append_frame(
                        output,
                        stderr,
                        limit=self._max_output_bytes,
                    )
                    or truncated
                )
        inspected = api.exec_inspect(exec_id)
        exit_code = (
            inspected.get("ExitCode")
            if isinstance(inspected, dict)
            else getattr(inspected, "exit_code", -1)
        )
        exit_code = int(exit_code if exit_code is not None else -1)
        decoded = output.decode("utf-8", errors="replace")
        if exit_code == 124:
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout} seconds",
                exit_code=-1,
                truncated=truncated,
            )
        return ExecuteResponse(output=decoded, exit_code=exit_code, truncated=truncated)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = self._effective_timeout(timeout)
        try:
            with self._adapter.operation(self._container):
                return self._exec_sync(command, effective_timeout)
        except Exception:
            return ExecuteResponse(
                output="Docker sandbox command failed", exit_code=-1, truncated=False
            )

    async def _recover_after_timeout(self, *, restart: bool) -> None:
        task = asyncio.create_task(
            run_blocking_io(
                self._adapter.recover_sandbox,
                self._container,
                restart,
            )
        )
        await asyncio.shield(task)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = self._effective_timeout(timeout)
        try:
            return await run_blocking_io(
                lambda: self.execute(command, timeout=timeout),
                timeout=effective_timeout + 10,
            )
        except asyncio.TimeoutError:
            try:
                await self._recover_after_timeout(restart=True)
            except Exception:
                logger.warning("Docker sandbox timeout recovery failed for container %s", self.id)
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout} seconds",
                exit_code=-1,
                truncated=False,
            )
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                run_blocking_io(self._adapter.recover_sandbox, self._container, False)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
            raise

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        timeout = get_sandbox_grep_timeout(settings)
        result = self.execute(build_grep_command(pattern, path, glob), timeout=timeout)
        return _grep_result(parse_grep_response(result, timeout), max_count)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        timeout = get_sandbox_grep_timeout(settings)
        result = await self.aexecute(build_grep_command(pattern, path, glob), timeout=timeout)
        return _grep_result(parse_grep_response(result, timeout), max_count)

    @staticmethod
    def _invalid_path(path: str) -> bool:
        if not path or "\x00" in path or not path.startswith("/"):
            return True
        return any(part == ".." for part in path.split("/"))

    @staticmethod
    def _map_file_error(exc: BaseException) -> str:
        message = str(exc).lower()
        if "permission" in message or "denied" in message:
            return "permission_denied"
        if "is a directory" in message or "directory" in message:
            return "is_directory"
        if "invalid" in message or "absolute" in message or "traversal" in message:
            return "invalid_path"
        return "file_not_found"

    @staticmethod
    def _tar_member_name(path: str) -> str:
        name = os.path.basename(path)
        if not name or name in {".", ".."}:
            raise ValueError("invalid_path")
        return name

    def _build_single_file_tar(self, path: str, content: bytes) -> SpooledTemporaryFile[bytes]:
        spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=SANDBOX_UPLOAD_MAX_BYTES)
        with tarfile.open(fileobj=spool, mode="w:") as archive:
            info = tarfile.TarInfo(self._tar_member_name(path))
            info.size = len(content)
            info.mode = 0o600
            info.uid = 65534
            info.gid = 65534
            archive.addfile(info, io.BytesIO(content))
        spool.seek(0)
        return spool

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if len(files) > SANDBOX_BATCH_FILES_LIMIT:
            return [file_upload_response(path=path, error="too_many_files") for path, _ in files]
        responses: list[FileUploadResponse] = []
        with self._adapter.operation(self._container):
            container = self._adapter.exec_client.containers.get(self.id)
            for original_path, content in files:
                path = self._resolve_path(original_path)
                if self._invalid_path(path):
                    responses.append(file_upload_response(path=path, error="invalid_path"))
                    continue
                if len(content) > SANDBOX_UPLOAD_MAX_BYTES:
                    responses.append(file_upload_response(path=path, error="file_too_large"))
                    continue
                spool: SpooledTemporaryFile[bytes] | None = None
                try:
                    parent = os.path.dirname(path) or "/"
                    spool = self._build_single_file_tar(path, content)
                    if not container.put_archive(parent, spool):
                        raise RuntimeError("Docker sandbox rejected file upload")
                    responses.append(file_upload_response(path=path))
                except Exception as exc:
                    responses.append(
                        file_upload_response(
                            path=path,
                            error=cast(ExtendedFileError, self._map_file_error(exc)),
                        )
                    )
                finally:
                    if spool is not None:
                        spool.close()
        return responses

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await run_blocking_io(self.upload_files, files)

    @staticmethod
    def _member_is_safe(member: tarfile.TarInfo) -> bool:
        name = member.name.replace("\\", "/")
        return (
            bool(name)
            and not name.startswith("/")
            and all(part not in {"", ".", ".."} for part in name.split("/"))
            and member.isfile()
        )

    def _read_archive(self, archive_data: Any, requested_path: str) -> bytes:
        spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(
            max_size=SANDBOX_DOWNLOAD_MAX_BYTES
        )
        total = 0
        try:
            if isinstance(archive_data, (bytes, bytearray, memoryview)):
                chunks: Iterable[Any] = [archive_data]
            else:
                chunks = archive_data if isinstance(archive_data, Iterable) else [archive_data]
            for chunk in chunks:
                if not chunk:
                    continue
                raw = bytes(chunk)
                total += len(raw)
                if total > SANDBOX_DOWNLOAD_MAX_BYTES:
                    raise OverflowError("file_too_large")
                spool.write(raw)
            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:*") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not self._member_is_safe(members[0]):
                    raise ValueError("invalid_path")
                extracted = archive.extractfile(members[0])
                if extracted is None:
                    raise IsADirectoryError(requested_path)
                content = extracted.read(SANDBOX_DOWNLOAD_MAX_BYTES + 1)
                if len(content) > SANDBOX_DOWNLOAD_MAX_BYTES:
                    raise OverflowError("file_too_large")
                return content
        finally:
            spool.close()

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        if len(paths) > SANDBOX_BATCH_FILES_LIMIT:
            return [file_download_response(path=path, error="too_many_files") for path in paths]
        responses: list[FileDownloadResponse] = []
        with self._adapter.operation(self._container):
            container = self._adapter.exec_client.containers.get(self.id)
            for original_path in paths:
                path = self._resolve_path(original_path)
                if self._invalid_path(path):
                    responses.append(file_download_response(path=path, error="invalid_path"))
                    continue
                try:
                    archive_data, _ = container.get_archive(path)
                    content = self._read_archive(archive_data, path)
                    responses.append(file_download_response(path=path, content=content))
                except (tarfile.ReadError, ValueError):
                    responses.append(file_download_response(path=path, error="invalid_path"))
                except OverflowError:
                    responses.append(file_download_response(path=path, error="file_too_large"))
                except Exception as exc:
                    responses.append(
                        file_download_response(
                            path=path,
                            error=cast(ExtendedFileError, self._map_file_error(exc)),
                        )
                    )
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await run_blocking_io(self.download_files, paths)


__all__ = [
    "DockerSandboxBackend",
    "SANDBOX_BATCH_FILES_LIMIT",
    "SANDBOX_DOWNLOAD_MAX_BYTES",
    "SANDBOX_UPLOAD_MAX_BYTES",
]

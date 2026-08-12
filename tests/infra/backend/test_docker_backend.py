from __future__ import annotations

import tarfile
from contextlib import contextmanager

from src.infra.backend.docker import DockerSandboxBackend


class _Api:
    def __init__(self, exit_code: int = 0, frames: list[object] | None = None) -> None:
        self.exit_code = exit_code
        self.frames = frames or []
        self.created: list[dict[str, object]] = []

    def exec_create(self, container_id: str, **kwargs: object) -> str:
        self.created.append({"container_id": container_id, **kwargs})
        return "exec-1"

    def exec_start(self, _exec_id: str, **_kwargs: object):
        return iter(self.frames)

    def exec_inspect(self, _exec_id: str) -> dict[str, int]:
        return {"ExitCode": self.exit_code}


class _ExecClient:
    def __init__(self, api: _Api) -> None:
        self.api = api


class _Container:
    id = "container-1"


class _Adapter:
    def __init__(self, api: _Api) -> None:
        self.exec_client = _ExecClient(api)

    @contextmanager
    def operation(self, _container: object):
        yield None

    def recover_sandbox(self, _container: object, _restart: bool) -> bool:
        return True


def test_execute_uses_low_level_exec_and_hard_byte_limit() -> None:
    api = _Api(frames=[(b"abc", b"stderr")])
    backend = DockerSandboxBackend(
        _Container(),
        _Adapter(api),
        timeout=180,
        max_output_bytes=4,
        env_vars={
            "HOME": "attacker-value",
            "CUSTOM": "value",
            "DOCKER_HOST": "tcp://attacker:2375",
            "DOCKER_TLS_VERIFY": "1",
        },
        work_dir="/tmp/lambchat-workspace/sessions/s1",
    )

    result = backend.execute("printf hello", timeout=400)

    assert result.exit_code == 0
    assert result.output == "abcs"
    assert result.truncated is True
    request = api.created[0]
    assert request["container_id"] == "container-1"
    assert request["workdir"] == "/tmp"
    assert request["environment"]["CUSTOM"] == "value"  # type: ignore[index]
    assert request["environment"]["HOME"] == "/tmp/lambchat-home"  # type: ignore[index]
    assert "DOCKER_HOST" not in request["environment"]  # type: ignore[operator]
    assert "DOCKER_TLS_VERIFY" not in request["environment"]  # type: ignore[operator]
    assert "mkdir -p" in request["cmd"][2]  # type: ignore[index]
    assert "timeout" in request["cmd"][2]  # type: ignore[index]


def test_file_tar_rejects_paths_that_could_escape_the_requested_file() -> None:
    assert DockerSandboxBackend._member_is_safe(tarfile.TarInfo("file.txt")) is True
    absolute = tarfile.TarInfo("/etc/passwd")
    symlink = tarfile.TarInfo("link")
    symlink.type = tarfile.SYMTYPE
    assert DockerSandboxBackend._member_is_safe(absolute) is False
    assert DockerSandboxBackend._member_is_safe(symlink) is False


def test_execute_maps_gnu_timeout_exit_code_to_public_error() -> None:
    api = _Api(exit_code=124)
    backend = DockerSandboxBackend(
        _Container(),
        _Adapter(api),
        timeout=5,
        max_output_bytes=1024,
    )

    result = backend.execute("sleep 30")

    assert result.exit_code == -1
    assert result.output == "Command timed out after 5 seconds"

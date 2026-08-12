"""Docker-specific user binding and session scoping for SessionSandboxManager."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from deepagents.backends import CompositeBackend

from src.infra.async_utils import run_blocking_io as _run_blocking_io
from src.infra.backend.skills_store import create_skills_backend
from src.infra.envvar.sync import sync_sandbox_env_vars
from src.infra.logging import get_logger
from src.infra.sandbox._docker_adapter import (
    CREATED_AT_LABEL,
    MANAGED_LABEL,
    NAMESPACE_LABEL,
    OWNER_HASH_LABEL,
    PLATFORM_LABEL,
    DockerSandboxAdapter,
)
from src.infra.sandbox.base import DockerSandboxConfig, get_docker_sandbox_config_from_settings
from src.infra.utils.datetime import utc_now_iso

logger = get_logger(__name__)


def run_blocking_io(*args, **kwargs):
    from src.infra.sandbox import session_manager

    return getattr(session_manager, "run_blocking_io", _run_blocking_io)(*args, **kwargs)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _created_at(container: Any) -> datetime:
    labels = dict(
        getattr(container, "labels", None)
        or (getattr(container, "attrs", {}) or {}).get("Config", {}).get("Labels", {})
        or {}
    )
    raw = labels.get(CREATED_AT_LABEL)
    if isinstance(raw, str):
        try:
            return _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass
    raw_created = (getattr(container, "attrs", {}) or {}).get("Created")
    if isinstance(raw_created, str):
        try:
            return _utc(datetime.fromisoformat(raw_created.replace("Z", "+00:00")))
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


class _DockerMixin:
    """Docker platform lifecycle methods mixed into the session manager."""

    if TYPE_CHECKING:
        _docker_adapter: Optional[DockerSandboxAdapter]
        _docker_namespace: str
        _cache: OrderedDict[str, tuple[str, CompositeBackend, object | None]]
        _bindings: Any

        def _get_user_lock(self, user_id: str) -> asyncio.Lock: ...

        async def _get_user_env_vars(self, user_id: str) -> dict[str, str]: ...

        def _session_work_dir(self, base_work_dir: str, session_id: str) -> str: ...

        async def _ensure_work_dir(self, backend: CompositeBackend, work_dir: str) -> None: ...

        def _evict_if_needed(self) -> None: ...

        def _binding_platform(self) -> str: ...

        def start_background_tasks(self) -> None: ...

        async def _cleanup_stale_docker_sandboxes(self) -> int: ...

    def _get_docker_config(self) -> DockerSandboxConfig:
        config = get_docker_sandbox_config_from_settings(self._docker_namespace)
        assert self._docker_adapter is not None
        self._docker_adapter.refresh_config(config)
        return config

    @staticmethod
    def _container_running(container: Any) -> bool:
        with_running = getattr(container, "attrs", {}) or {}
        state = with_running.get("State") or {}
        status = state.get("Status", getattr(container, "status", ""))
        return bool(state.get("Running", str(status).lower() == "running"))

    @staticmethod
    def _container_labels(container: Any) -> dict[str, str]:
        return dict(
            getattr(container, "labels", None)
            or (getattr(container, "attrs", {}) or {}).get("Config", {}).get("Labels", {})
            or {}
        )

    @staticmethod
    def _owner_hash(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def _is_docker_container_owned_by(
        self,
        container: Any,
        user_id: str,
        config: DockerSandboxConfig,
    ) -> bool:
        labels = self._container_labels(container)
        return (
            labels.get(MANAGED_LABEL) == "true"
            and labels.get(PLATFORM_LABEL) == "docker"
            and labels.get(NAMESPACE_LABEL) == config.namespace
            and labels.get(OWNER_HASH_LABEL) == self._owner_hash(user_id)
        )

    async def _get_docker_binding(self, user_id: str) -> dict[str, Any] | None:
        """Read only the nested Docker slot; never consume legacy top-level IDs."""

        doc = await self._bindings.find_one({"user_id": user_id})
        if not doc:
            return None
        nested = (doc.get("sandboxes") or {}).get("docker")
        if not isinstance(nested, dict):
            return None
        result = dict(doc)
        result.update(nested)
        return result

    async def _save_docker_binding(
        self,
        user_id: str,
        container_id: str,
        state: str,
        *,
        is_new: bool = False,
    ) -> None:
        now = utc_now_iso()
        update: dict[str, Any] = {
            "$set": {
                "sandboxes.docker.container_id": container_id,
                "sandboxes.docker.sandbox_id": container_id,
                "sandboxes.docker.sandbox_platform": "docker",
                "sandboxes.docker.sandbox_state": state,
                "sandboxes.docker.sandbox_last_used_at": now,
            },
        }
        if self._binding_platform() == "docker":
            update["$set"].update(
                {
                    "sandbox_platform": "docker",
                    "sandbox_id": container_id,
                    "sandbox_state": state,
                    "sandbox_last_used_at": now,
                }
            )
        if is_new:
            update["$set"].update(
                {
                    "sandbox_created_at": now,
                    "sandboxes.docker.sandbox_created_at": now,
                }
            )
        else:
            update["$setOnInsert"] = {
                "sandbox_created_at": now,
                "sandboxes.docker.sandbox_created_at": now,
            }
        await self._bindings.update_one({"user_id": user_id}, update, upsert=True)

    async def _load_docker_container(self, container_id: str) -> Any | None:
        assert self._docker_adapter is not None
        try:
            return await run_blocking_io(self._docker_adapter.get_sandbox, container_id)
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "outside the managed namespace" in message:
                return None
            raise

    async def _start_and_touch_docker(self, container: Any) -> None:
        assert self._docker_adapter is not None
        if not self._container_running(container):
            await run_blocking_io(self._docker_adapter.start_sandbox, container)
        if not await run_blocking_io(
            self._docker_adapter.touch_if_available,
            self._docker_adapter.get_sandbox_id(container),
        ):
            raise RuntimeError("Docker sandbox is recovering or unavailable")

    async def _remove_docker_if_claimed(self, container: Any) -> bool:
        assert self._docker_adapter is not None
        container_id = self._docker_adapter.get_sandbox_id(container)
        claimed = await run_blocking_io(self._docker_adapter.claim_for_removal, container_id)
        if not claimed:
            return False
        return await run_blocking_io(self._docker_adapter.remove_sandbox, container_id)

    def _build_docker_composite(
        self,
        container: Any,
        user_id: str,
        config: DockerSandboxConfig,
        env_vars: dict[str, str],
    ) -> CompositeBackend:
        assert self._docker_adapter is not None
        from src.infra.backend.docker import DockerSandboxBackend

        base_backend = DockerSandboxBackend(
            container,
            self._docker_adapter,
            timeout=config.timeout,
            max_output_bytes=config.max_output_bytes,
            env_vars=env_vars,
        )
        return CompositeBackend(
            default=base_backend,
            routes={"/skills/": create_skills_backend(user_id=user_id)},
            artifacts_root="/tmp/lambchat-workspace",
        )

    def _scope_docker_backend(
        self,
        backend: CompositeBackend,
        user_id: str,
        config: DockerSandboxConfig,
        work_dir: str,
    ) -> CompositeBackend:
        from src.infra.backend.docker import DockerSandboxBackend

        assert self._docker_adapter is not None
        default = backend.default
        container = getattr(default, "_container", None)
        if container is None:
            cached = self._cache.get(user_id)
            if cached is None or cached[2] is None:
                raise RuntimeError("Docker sandbox container is unavailable for session scoping")
            container = cached[2]
        env_vars = dict(getattr(default, "env_vars", {}) or {})
        scoped = DockerSandboxBackend(
            container,
            self._docker_adapter,
            timeout=config.timeout,
            max_output_bytes=config.max_output_bytes,
            env_vars=env_vars,
            work_dir=work_dir,
        )
        return CompositeBackend(
            default=scoped,
            routes={"/skills/": create_skills_backend(user_id=user_id)},
            artifacts_root="/tmp/lambchat-workspace",
        )

    async def _prepare_docker_session(
        self,
        session_id: str,
        user_id: str,
        container: Any,
        backend: CompositeBackend,
        config: DockerSandboxConfig,
    ) -> tuple[CompositeBackend, str]:
        assert self._docker_adapter is not None
        base_work_dir = self._docker_adapter.work_dir(container)
        work_dir = self._session_work_dir(base_work_dir, session_id)
        scoped_backend = self._scope_docker_backend(backend, user_id, config, work_dir)
        await self._ensure_work_dir(scoped_backend, work_dir)
        await sync_sandbox_env_vars(scoped_backend, user_id)
        container_id = self._docker_adapter.get_sandbox_id(container)
        await self._save_docker_binding(user_id, container_id, "running")
        return scoped_backend, work_dir

    async def _find_docker_label_candidate(
        self,
        user_id: str,
        config: DockerSandboxConfig,
        *,
        exclude_id: str | None = None,
        return_deferred: bool = False,
    ) -> Any | None | tuple[Any | None, bool]:
        """Return a compatible candidate, optionally reporting deferred mismatch."""
        assert self._docker_adapter is not None
        owner_hash = self._owner_hash(user_id)
        candidates = await run_blocking_io(self._docker_adapter.list_sandboxes)
        compatible: list[Any] = []
        deferred: list[Any] = []
        for candidate in candidates:
            container_id = self._docker_adapter.get_sandbox_id(candidate)
            labels = self._container_labels(candidate)
            if container_id == exclude_id:
                continue
            if (
                labels.get(MANAGED_LABEL) != "true"
                or labels.get(PLATFORM_LABEL) != "docker"
                or labels.get(NAMESPACE_LABEL) != config.namespace
                or labels.get(OWNER_HASH_LABEL) != owner_hash
            ):
                continue
            if self._docker_adapter.is_config_compatible(candidate):
                compatible.append(candidate)
            elif not await self._remove_docker_if_claimed(candidate):
                deferred.append(candidate)
        if compatible:
            selected = max(compatible, key=_created_at)
            for duplicate in compatible:
                if duplicate is not selected:
                    await self._remove_docker_if_claimed(duplicate)
            return (selected, False) if return_deferred else selected
        if deferred:
            selected = max(deferred, key=_created_at)
            return (selected, True) if return_deferred else selected
        return (None, False) if return_deferred else None

    async def _get_or_create_docker(
        self,
        session_id: str,
        user_id: str,
    ) -> tuple[CompositeBackend, str]:
        assert self._docker_adapter is not None
        config = self._get_docker_config()
        self.start_background_tasks()
        lock = self._get_user_lock(user_id)
        async with lock:
            await self._cleanup_stale_docker_sandboxes()
            user_envs = await self._get_user_env_vars(user_id)

            cached = self._cache.get(user_id)
            if cached is not None:
                container_id, backend, _ = cached
                container = await self._load_docker_container(container_id)
                if container is not None and self._is_docker_container_owned_by(
                    container, user_id, config
                ):
                    try:
                        if not self._docker_adapter.is_config_compatible(container):
                            if not await self._remove_docker_if_claimed(container):
                                logger.info(
                                    "Docker config change deferred for active container %s",
                                    container_id,
                                )
                                await self._start_and_touch_docker(container)
                                self._cache[user_id] = (container_id, backend, container)
                                self._cache.move_to_end(user_id)
                                return await self._prepare_docker_session(
                                    session_id, user_id, container, backend, config
                                )
                            self._cache.pop(user_id, None)
                            container = None
                        if container is not None:
                            await self._start_and_touch_docker(container)
                            self._cache[user_id] = (container_id, backend, container)
                            self._cache.move_to_end(user_id)
                            return await self._prepare_docker_session(
                                session_id, user_id, container, backend, config
                            )
                    except Exception as exc:
                        logger.warning(
                            "Docker cache reconnect failed for owner hash=%s: %s",
                            self._owner_hash(user_id)[:12],
                            exc,
                        )
                self._cache.pop(user_id, None)

            binding = await self._get_docker_binding(user_id)
            binding_id = None
            if binding:
                binding_id = binding.get("container_id") or binding.get("sandbox_id")
            container = await self._load_docker_container(str(binding_id)) if binding_id else None
            if container is not None and self._is_docker_container_owned_by(
                container, user_id, config
            ):
                try:
                    if not self._docker_adapter.is_config_compatible(container):
                        if not await self._remove_docker_if_claimed(container):
                            logger.info(
                                "Docker config change deferred for active bound container %s",
                                binding_id,
                            )
                            await self._start_and_touch_docker(container)
                            backend = self._build_docker_composite(
                                container, user_id, config, user_envs
                            )
                            container_id = self._docker_adapter.get_sandbox_id(container)
                            self._cache[user_id] = (container_id, backend, container)
                            self._evict_if_needed()
                            return await self._prepare_docker_session(
                                session_id, user_id, container, backend, config
                            )
                        container = None
                    if container is not None:
                        await self._start_and_touch_docker(container)
                        backend = self._build_docker_composite(
                            container, user_id, config, user_envs
                        )
                        container_id = self._docker_adapter.get_sandbox_id(container)
                        self._cache[user_id] = (container_id, backend, container)
                        self._evict_if_needed()
                        return await self._prepare_docker_session(
                            session_id, user_id, container, backend, config
                        )
                except Exception as exc:
                    logger.warning(
                        "Docker binding reconnect failed for owner hash=%s: %s",
                        self._owner_hash(user_id)[:12],
                        exc,
                    )

            candidate = await self._find_docker_label_candidate(
                user_id,
                config,
                return_deferred=True,
            )
            if isinstance(candidate, tuple):
                container, config_change_deferred = candidate
            else:
                container, config_change_deferred = candidate, False
            if container is None:
                container = await run_blocking_io(self._docker_adapter.create_sandbox, user_id)
            await self._start_and_touch_docker(container)
            container_id = self._docker_adapter.get_sandbox_id(container)
            backend = self._build_docker_composite(container, user_id, config, user_envs)
            try:
                await self._save_docker_binding(
                    user_id,
                    container_id,
                    "running",
                    is_new=binding is None,
                )
            except Exception:
                if not config_change_deferred:
                    await run_blocking_io(
                        self._docker_adapter.remove_sandbox, container_id, force=True
                    )
                raise
            self._cache[user_id] = (container_id, backend, container)
            self._evict_if_needed()
            return await self._prepare_docker_session(
                session_id, user_id, container, backend, config
            )

    async def _stop_docker(self, user_id: str) -> bool:
        assert self._docker_adapter is not None
        config = self._get_docker_config()
        lock = self._get_user_lock(user_id)
        async with lock:
            cached = self._cache.get(user_id)
            container_id = cached[0] if cached else None
            if container_id is None:
                binding = await self._get_docker_binding(user_id)
                container_id = (binding or {}).get("container_id") or (binding or {}).get(
                    "sandbox_id"
                )
            if not container_id:
                return False
            container = await self._load_docker_container(str(container_id))
            if container is None or not self._is_docker_container_owned_by(
                container, user_id, config
            ):
                self._cache.pop(user_id, None)
                return False
            stopped = await run_blocking_io(self._docker_adapter.recover_sandbox, container, False)
            if not stopped:
                return False
            self._cache.pop(user_id, None)
            await self._save_docker_binding(user_id, str(container_id), "stopped")
            return True

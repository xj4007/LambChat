"""Restricted Docker Engine adapter for local LambChat sandboxes.

The adapter is the only module that talks to the Docker SDK.  It deliberately
keeps Docker optional at import time: ordinary deployments can import the
sandbox manager without contacting (or requiring access to) a daemon.
"""

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List

from src.infra.logging import get_logger
from src.kernel.config.docker_sandbox import (
    DOCKER_SANDBOX_CONTRACT_VERSION,
    validate_docker_sandbox_values,
)

from .base import DockerSandboxConfig

logger = get_logger(__name__)

MANAGED_LABEL = "io.lambchat.sandbox.managed"
PLATFORM_LABEL = "io.lambchat.sandbox.platform"
NAMESPACE_LABEL = "io.lambchat.sandbox.namespace"
OWNER_HASH_LABEL = "io.lambchat.sandbox.owner_hash"
CONFIG_HASH_LABEL = "io.lambchat.sandbox.config_hash"
CONTAINER_TOKEN_LABEL = "io.lambchat.sandbox.container_token"
CREATED_AT_LABEL = "io.lambchat.sandbox.created_at"

NETWORK_PREFIX = "lambchat-sbx-net-"
CONTAINER_PREFIX = "lambchat-sbx-"

_FIXED_ENV = {
    "HOME": "/tmp/lambchat-home",
    "PYTHONUSERBASE": "/tmp/lambchat-home/.local",
    "PIP_USER": "1",
    "PATH": "/tmp/lambchat-home/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
}
_KEEPALIVE_COMMAND = (
    'sh -lc "mkdir -p \\"$HOME\\" \\"$PYTHONUSERBASE\\" /tmp/lambchat-workspace && '
    "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"
    '"'
)
_PRELIGHT_COMMAND = (
    "set -eu; command -v sh >/dev/null; python3 -m pip --version >/dev/null; "
    "for command in timeout sleep mkdir stat find grep head tail cat wc tr rm; do "
    'command -v "$command" >/dev/null; done; '
    "mkdir -p /tmp/lambchat-workspace; "
    "test -w /tmp/lambchat-workspace; "
    "touch /tmp/lambchat-workspace/.lambchat-preflight; "
    "rm -f /tmp/lambchat-workspace/.lambchat-preflight"
)
_PREFLIGHT_COMMAND = _PRELIGHT_COMMAND


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def container_config_hash(config: DockerSandboxConfig) -> str:
    """Return the stable hash of the container-creation safety contract."""

    payload = {
        "contract_version": DOCKER_SANDBOX_CONTRACT_VERSION,
        "image": config.image,
        "memory_limit_mb": config.memory_limit_mb,
        "cpu_limit": config.cpu_limit,
        "pids_limit": config.pids_limit,
        "network_mode": config.network_mode,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class _OperationState:
    """Per-container gate shared by commands, recovery, and janitor removal."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    condition: threading.Condition = field(init=False)
    active_count: int = 0
    recovering: bool = False
    available: bool = True
    last_activity: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


class DockerSandboxAdapter:
    """Synchronous, fail-closed boundary around the Docker SDK."""

    def __init__(self, config: DockerSandboxConfig) -> None:
        validate_docker_sandbox_values(
            {
                "DOCKER_SANDBOX_NAMESPACE": config.namespace,
                "DOCKER_SANDBOX_IMAGE": config.image,
                "DOCKER_SANDBOX_TIMEOUT": config.timeout,
                "DOCKER_SANDBOX_IDLE_TIMEOUT": config.idle_timeout,
                "DOCKER_SANDBOX_CLEANUP_INTERVAL": config.cleanup_interval,
                "DOCKER_SANDBOX_MAX_CONTAINERS": config.max_containers,
                "DOCKER_SANDBOX_MEMORY_LIMIT_MB": config.memory_limit_mb,
                "DOCKER_SANDBOX_CPU_LIMIT": config.cpu_limit,
                "DOCKER_SANDBOX_PIDS_LIMIT": config.pids_limit,
                "DOCKER_SANDBOX_NETWORK_MODE": config.network_mode,
                "DOCKER_SANDBOX_MAX_OUTPUT_BYTES": config.max_output_bytes,
            }
        )
        self.config = config
        self._client_lock = threading.RLock()
        self._creation_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._states: dict[str, _OperationState] = {}
        self._docker: Any | None = None
        self._management_client: Any | None = None
        self._exec_client: Any | None = None
        self._control_client: Any | None = None
        self._daemon_info: dict[str, Any] | None = None
        self._validate_transport()

    @property
    def namespace(self) -> str:
        return self.config.namespace

    @property
    def config_hash(self) -> str:
        return container_config_hash(self.config)

    @property
    def management_client(self) -> Any:
        return self._ensure_clients()[0]

    @property
    def exec_client(self) -> Any:
        return self._ensure_clients()[1]

    @property
    def control_client(self) -> Any:
        return self._ensure_clients()[2]

    def _validate_transport(self) -> None:
        host = os.environ.get("DOCKER_HOST", "").strip().lower()
        if (
            host.startswith(("tcp://", "http://"))
            and not os.environ.get("DOCKER_TLS_VERIFY", "").strip()
        ):
            raise ValueError("Docker sandbox refuses an unauthenticated TCP Docker endpoint")

    def _load_docker(self) -> Any:
        if self._docker is None:
            try:
                import docker  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - dependency is in production lock
                raise RuntimeError("Docker sandbox requires the Python Docker SDK") from exc
            self._docker = docker
        return self._docker

    def _new_client(self, timeout: int) -> Any:
        docker = self._load_docker()
        factory = getattr(docker, "from_env", None)
        if factory is None:
            factory = docker.DockerClient.from_env
        return factory(timeout=timeout, version="auto")

    def _ensure_clients(self) -> tuple[Any, Any, Any]:
        with self._client_lock:
            if self._management_client is not None:
                return (
                    self._management_client,
                    self._exec_client,
                    self._control_client,
                )

            management = None
            execution = None
            control = None
            try:
                management = self._new_client(120)
                execution = self._new_client(max(120, self.config.timeout + 15))
                control = self._new_client(15)
                management.ping()
                info = dict(management.info())
                self._validate_daemon_info(info)
            except Exception:
                for client in (management, execution, control):
                    if client is not None:
                        with contextlib.suppress(Exception):
                            client.close()
                raise

            assert management is not None
            assert execution is not None
            assert control is not None
            self._management_client = management
            self._exec_client = execution
            self._control_client = control
            self._daemon_info = info
            return management, execution, control

    def _validate_daemon_info(self, info: dict[str, Any]) -> None:
        missing: list[str] = []
        if str(info.get("OSType", "")).lower() != "linux":
            missing.append("linux")
        for key, label in (
            ("MemoryLimit", "memory_limit"),
            ("SwapLimit", "swap_limit"),
            ("CpuCfsQuota", "cpu_cfs_quota"),
            ("PidsLimit", "pids_limit"),
        ):
            if info.get(key) is not True:
                missing.append(label)
        security_options = info.get("SecurityOptions") or []
        has_seccomp = any("seccomp" in str(option).lower() for option in security_options)
        if not has_seccomp:
            missing.append("seccomp")
        if missing:
            raise RuntimeError(
                "Docker sandbox daemon lacks required Linux isolation capabilities: "
                + ", ".join(missing)
            )

    def close(self) -> None:
        with self._client_lock:
            clients = (
                self._management_client,
                self._exec_client,
                self._control_client,
            )
            self._management_client = None
            self._exec_client = None
            self._control_client = None
            self._daemon_info = None
            for client in clients:
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.close()

    def _labels_for_filter(self) -> list[str]:
        return [
            f"{MANAGED_LABEL}=true",
            f"{PLATFORM_LABEL}=docker",
            f"{NAMESPACE_LABEL}={self.namespace}",
        ]

    @staticmethod
    def _object_id(value: object) -> str:
        identifier = getattr(value, "id", value)
        return str(identifier)

    @staticmethod
    def _is_not_found(exc: BaseException) -> bool:
        return (
            type(exc).__name__.lower() in {"notfound", "imagenotfound"}
            or "not found" in str(exc).lower()
        )

    def list_sandboxes(self, *, include_stopped: bool = True) -> list[Any]:
        client = self.management_client
        containers = client.containers.list(
            all=include_stopped,
            filters={"label": self._labels_for_filter()},
        )
        result: list[Any] = []
        for container in containers:
            reload = getattr(container, "reload", None)
            if callable(reload):
                try:
                    reload()
                except Exception as exc:
                    if self._is_not_found(exc):
                        continue
                    raise
            attrs = getattr(container, "attrs", {}) or {}
            labels = dict(
                getattr(container, "labels", None)
                or attrs.get("Config", {}).get("Labels", {})
                or {}
            )
            if labels.get(MANAGED_LABEL) != "true":
                continue
            if (
                labels.get(PLATFORM_LABEL) != "docker"
                or labels.get(NAMESPACE_LABEL) != self.namespace
            ):
                continue
            result.append(container)
        return result

    def count_sandboxes(self, *, include_stopped: bool = True) -> int:
        """Count only managed containers in this adapter namespace."""
        return len(self.list_sandboxes(include_stopped=include_stopped))

    def get_sandbox(self, container_id: str) -> Any:
        container = self.management_client.containers.get(container_id)
        attrs = getattr(container, "attrs", {}) or {}
        labels = dict(
            getattr(container, "labels", None) or attrs.get("Config", {}).get("Labels", {}) or {}
        )
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(PLATFORM_LABEL) != "docker"
            or labels.get(NAMESPACE_LABEL) != self.namespace
        ):
            raise ValueError("Docker sandbox container is outside the managed namespace")
        return container

    def get_container(self, container_id: str) -> Any:
        return self.get_sandbox(container_id)

    def get_sandbox_id(self, container: Any) -> str:
        return self._object_id(container)

    def work_dir(self, _container: Any | None = None) -> str:
        return "/tmp/lambchat-workspace"

    def info(self, container: Any) -> dict[str, Any]:
        try:
            container.reload()
        except Exception as exc:
            if not self._is_not_found(exc):
                raise
        attrs = dict(getattr(container, "attrs", {}) or {})
        return {
            "sandbox_id": self._object_id(container),
            "container_id": self._object_id(container),
            "state": (attrs.get("State") or {}).get(
                "Status", getattr(container, "status", "unknown")
            ),
            "labels": dict(
                getattr(container, "labels", None)
                or (attrs.get("Config") or {}).get("Labels", {})
                or {}
            ),
            "created_at": (attrs.get("Created") or None),
            "work_dir": self.work_dir(container),
        }

    def get_info(self, container: Any) -> dict[str, Any]:
        return self.info(container)

    def _state_for(self, container_id: str, *, create: bool = True) -> _OperationState | None:
        with self._state_lock:
            state = self._states.get(container_id)
            if state is None and create:
                state = _OperationState()
                self._states[container_id] = state
            return state

    def get_operation_state(self, container_id: str) -> _OperationState | None:
        """Return existing operation state without creating a janitor timestamp."""
        return self._state_for(container_id, create=False)

    @contextlib.contextmanager
    def operation(self, container: Any) -> Iterator[_OperationState]:
        """Gate one container operation; queued calls cannot cross recovery."""

        container_id = self._object_id(container)
        state = self._state_for(container_id)
        assert state is not None
        with state.condition:
            while state.recovering:
                state.condition.wait()
            if not state.available:
                raise RuntimeError("Docker sandbox is unavailable")
            state.active_count += 1
            state.last_activity = _utc_now()
        try:
            yield state
        finally:
            with state.condition:
                state.active_count = max(0, state.active_count - 1)
                state.last_activity = _utc_now()
                state.condition.notify_all()

    @contextlib.contextmanager
    def operation_for_id(self, container_id: str) -> Iterator[_OperationState]:
        container = self.get_sandbox(container_id)
        with self.operation(container) as state:
            yield state

    def create(self, owner_id: str) -> Any:
        return self.create_sandbox(owner_id)

    def get(self, container_id: str) -> Any:
        return self.get_sandbox(container_id)

    def list(self, *, include_stopped: bool = True) -> list[Any]:
        return self.list_sandboxes(include_stopped=include_stopped)

    def id(self, container: Any) -> str:
        return self.get_sandbox_id(container)

    def workdir(self, container: Any | None = None) -> str:
        return self.work_dir(container)

    def is_config_compatible(self, container: Any) -> bool:
        labels = dict(
            getattr(container, "labels", None)
            or (getattr(container, "attrs", {}) or {}).get("Config", {}).get("Labels", {})
            or {}
        )
        return labels.get(CONFIG_HASH_LABEL) == self.config_hash

    def refresh_config(self, config: DockerSandboxConfig) -> None:
        """Refresh mutable settings and only rebuild the exec client timeout."""

        validate_docker_sandbox_values(
            {
                "DOCKER_SANDBOX_NAMESPACE": config.namespace,
                "DOCKER_SANDBOX_IMAGE": config.image,
                "DOCKER_SANDBOX_TIMEOUT": config.timeout,
                "DOCKER_SANDBOX_IDLE_TIMEOUT": config.idle_timeout,
                "DOCKER_SANDBOX_CLEANUP_INTERVAL": config.cleanup_interval,
                "DOCKER_SANDBOX_MAX_CONTAINERS": config.max_containers,
                "DOCKER_SANDBOX_MEMORY_LIMIT_MB": config.memory_limit_mb,
                "DOCKER_SANDBOX_CPU_LIMIT": config.cpu_limit,
                "DOCKER_SANDBOX_PIDS_LIMIT": config.pids_limit,
                "DOCKER_SANDBOX_NETWORK_MODE": config.network_mode,
                "DOCKER_SANDBOX_MAX_OUTPUT_BYTES": config.max_output_bytes,
            }
        )
        with self._client_lock:
            old_timeout = self.config.timeout
            replacement = None
            if self._exec_client is not None and old_timeout != config.timeout:
                replacement = self._new_client(max(120, config.timeout + 15))
            old_client = self._exec_client
            self.config = config
            if replacement is not None:
                self._exec_client = replacement
                if old_client is not None:
                    with contextlib.suppress(Exception):
                        old_client.close()

    reload_config = refresh_config

    def touch_if_available(self, container_id: str) -> bool:
        state = self._state_for(container_id, create=False)
        if state is None:
            state = self._state_for(container_id)
        assert state is not None
        with state.condition:
            if state.recovering or not state.available:
                return False
            state.last_activity = _utc_now()
            state.condition.notify_all()
            return True

    def _ensure_image(self) -> None:
        client = self.management_client
        try:
            client.images.get(self.config.image)
        except Exception as exc:
            docker = self._load_docker()
            image_not_found = getattr(getattr(docker, "errors", None), "ImageNotFound", ())
            if image_not_found and isinstance(exc, image_not_found):
                client.images.pull(self.config.image)
                return
            if type(exc).__name__ == "ImageNotFound":
                client.images.pull(self.config.image)
                return
            raise

    def _network_labels(self, token: str, created_at: str) -> dict[str, str]:
        return {
            MANAGED_LABEL: "true",
            PLATFORM_LABEL: "docker",
            NAMESPACE_LABEL: self.namespace,
            CONTAINER_TOKEN_LABEL: token,
            CREATED_AT_LABEL: created_at,
        }

    def _new_network(self, token: str, created_at: str) -> Any:
        name = f"{NETWORK_PREFIX}{self.namespace}-{token[:12]}"
        return self.management_client.networks.create(
            name=name,
            driver="bridge",
            internal=False,
            attachable=False,
            labels=self._network_labels(token, created_at),
        )

    def _container_kwargs(
        self,
        *,
        name: str,
        labels: dict[str, str],
        network: Any | None,
    ) -> dict[str, Any]:
        docker = self._load_docker()
        try:
            log_config = docker.types.LogConfig(type="none")
        except Exception:
            log_config = {"Type": "none"}
        kwargs: dict[str, Any] = {
            "name": name,
            "detach": True,
            "auto_remove": False,
            "init": True,
            "user": "65534:65534",
            "working_dir": "/tmp",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": f"{self.config.memory_limit_mb}m",
            "memswap_limit": f"{self.config.memory_limit_mb}m",
            "nano_cpus": int(self.config.cpu_limit * 1_000_000_000),
            "pids_limit": self.config.pids_limit,
            "restart_policy": {"Name": "no"},
            "log_config": log_config,
            "stdin_open": False,
            "tty": False,
            "environment": dict(_FIXED_ENV),
            "labels": labels,
            "command": _KEEPALIVE_COMMAND,
        }
        if self.config.network_mode == "none":
            kwargs["network_mode"] = "none"
        else:
            assert network is not None
            kwargs["network"] = getattr(network, "name", self._object_id(network))
        return kwargs

    @staticmethod
    def _exec_id(created: Any) -> str:
        if isinstance(created, dict):
            created = created.get("Id") or created.get("id")
        if not isinstance(created, str) or not created:
            raise RuntimeError("Docker sandbox preflight did not return an exec ID")
        return created

    @staticmethod
    def _exec_exit_code(inspected: Any) -> int:
        if isinstance(inspected, tuple):
            exit_code = inspected[0] if inspected else None
        elif isinstance(inspected, dict):
            exit_code = inspected.get("ExitCode")
        else:
            exit_code = getattr(inspected, "exit_code", None)
        return int(exit_code if exit_code is not None else -1)

    def _preflight(self, container: Any) -> None:
        api = getattr(self.management_client, "api", None)
        if api is None:
            result = container.exec_run(
                ["sh", "-lc", _PREFLIGHT_COMMAND],
                user="65534:65534",
                workdir="/tmp",
                stdout=True,
                stderr=True,
                demux=False,
            )
            if self._exec_exit_code(result) != 0:
                raise RuntimeError("Docker sandbox image failed the required command preflight")
            return
        created = api.exec_create(
            self._object_id(container),
            cmd=["sh", "-lc", _PREFLIGHT_COMMAND],
            user="65534:65534",
            workdir="/tmp",
            stdout=True,
            stderr=True,
        )
        exec_id = self._exec_id(created)
        api.exec_start(exec_id, stream=False, demux=False)
        if self._exec_exit_code(api.exec_inspect(exec_id)) != 0:
            raise RuntimeError("Docker sandbox image failed the required command preflight")

    def create_sandbox(self, owner_id: str) -> Any:
        owner_hash = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()
        token = str(uuid.uuid4())
        created_at = _utc_now().isoformat()
        labels = {
            MANAGED_LABEL: "true",
            PLATFORM_LABEL: "docker",
            NAMESPACE_LABEL: self.namespace,
            OWNER_HASH_LABEL: owner_hash,
            CONFIG_HASH_LABEL: self.config_hash,
            CONTAINER_TOKEN_LABEL: token,
            CREATED_AT_LABEL: created_at,
        }
        name = f"{CONTAINER_PREFIX}{self.namespace}-{owner_hash[:12]}-{token[:12]}"
        with self._creation_lock:
            if self.count_sandboxes() >= self.config.max_containers:
                raise RuntimeError(
                    f"Docker sandbox capacity reached ({self.config.max_containers} managed containers)"
                )
            self._ensure_image()
            network: Any | None = None
            container: Any | None = None
            try:
                if self.config.network_mode == "bridge":
                    network = self._new_network(token, created_at)
                container = self.management_client.containers.run(
                    self.config.image,
                    **self._container_kwargs(name=name, labels=labels, network=network),
                )
                self._preflight(container)
                state = self._state_for(self._object_id(container))
                assert state is not None
                with state.condition:
                    state.last_activity = _as_utc(datetime.fromisoformat(created_at))
                    state.available = True
                logger.info(
                    "Created Docker sandbox container=%s owner_hash=%s namespace=%s",
                    self._object_id(container),
                    owner_hash[:12],
                    self.namespace,
                )
                return container
            except Exception:
                if container is not None:
                    with contextlib.suppress(Exception):
                        container.remove(force=True)
                if network is not None:
                    with contextlib.suppress(Exception):
                        network.remove()
                if container is not None:
                    with self._state_lock:
                        self._states.pop(self._object_id(container), None)
                raise

    def _container_network_names(self, container: Any) -> List[str]:
        with contextlib.suppress(Exception):
            container.reload()
        attrs = dict(getattr(container, "attrs", {}) or {})
        networks = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
        return [str(name) for name in networks]

    def _remove_container_networks(self, network_names: List[str], token: str) -> None:
        for network_name in network_names:
            try:
                network = self.management_client.networks.get(network_name)
                reload = getattr(network, "reload", None)
                if callable(reload):
                    reload()
                attrs = dict(getattr(network, "attrs", {}) or {})
                labels = dict(getattr(network, "labels", None) or attrs.get("Labels", {}) or {})
                if (
                    labels.get(MANAGED_LABEL) == "true"
                    and labels.get(PLATFORM_LABEL) == "docker"
                    and labels.get(NAMESPACE_LABEL) == self.namespace
                    and labels.get(CONTAINER_TOKEN_LABEL) == token
                ):
                    network.remove()
            except Exception as exc:
                if not self._is_not_found(exc):
                    logger.warning(
                        "Failed to remove Docker sandbox network %s: %s", network_name, exc
                    )

    def _mark_recovering(self, container_id: str, *, allow_active: bool) -> _OperationState | None:
        state = self._state_for(container_id, create=False)
        if state is None:
            state = self._state_for(container_id)
        assert state is not None
        with state.condition:
            if state.recovering or not state.available:
                return None
            if not allow_active and state.active_count:
                return None
            state.recovering = True
            state.available = False
            state.condition.notify_all()
            return state

    @staticmethod
    def _wait_for_operations_to_drain(state: _OperationState, timeout_seconds: float = 10) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with state.condition:
            while state.active_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                state.condition.wait(timeout=remaining)
            return True

    def _container_running(self, container: Any) -> bool:
        with contextlib.suppress(Exception):
            container.reload()
        attrs = dict(getattr(container, "attrs", {}) or {})
        state = attrs.get("State") or {}
        status = state.get("Status", getattr(container, "status", ""))
        return bool(state.get("Running", str(status).lower() == "running"))

    def _control_container(self, container: Any) -> Any:
        """Resolve the same container through the short-timeout control client."""

        try:
            return self.control_client.containers.get(self._object_id(container))
        except Exception:
            return container

    def _stop_or_kill(self, container: Any) -> bool:
        control_container = self._control_container(container)
        try:
            control_container.stop(timeout=10)
        except Exception:
            with contextlib.suppress(Exception):
                control_container.kill()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self._container_running(control_container):
                return True
            time.sleep(0.05)
        with contextlib.suppress(Exception):
            control_container.kill()
        return not self._container_running(control_container)

    def recover_sandbox(self, container: Any, restart: bool) -> bool:
        container_id = self._object_id(container)
        state = self._mark_recovering(container_id, allow_active=True)
        if state is None:
            return not restart and not self._container_running(container)
        success = False
        try:
            control_container = self._control_container(container)
            success = self._stop_or_kill(control_container)
            if success:
                success = self._wait_for_operations_to_drain(state)
            if success and restart:
                control_container.start()
                success = self._container_running(control_container)
            return success
        finally:
            with state.condition:
                state.recovering = False
                state.available = bool(success and restart)
                state.last_activity = _utc_now()
                state.condition.notify_all()

    def start_sandbox(self, container: Any) -> bool:
        container_id = self._object_id(container)
        state = self._state_for(container_id)
        assert state is not None
        with state.condition:
            while state.recovering or state.active_count:
                state.condition.wait()
            state.recovering = True
            state.available = False
            state.condition.notify_all()
        success = False
        try:
            control_container = self._control_container(container)
            if not self._container_running(control_container):
                control_container.start()
            success = self._container_running(control_container)
            return success
        finally:
            with state.condition:
                state.recovering = False
                state.available = success
                state.last_activity = _utc_now()
                state.condition.notify_all()

    def stop_sandbox(self, container: Any) -> bool:
        return self.recover_sandbox(container, restart=False)

    def kill_sandbox(self, container: Any) -> bool:
        container_id = self._object_id(container)
        state = self._mark_recovering(container_id, allow_active=True)
        if state is None:
            return False
        success = False
        try:
            control_container = self._control_container(container)
            control_container.kill()
            success = not self._container_running(control_container)
            if success:
                success = self._wait_for_operations_to_drain(state)
            return success
        finally:
            with state.condition:
                state.recovering = False
                state.available = False
                state.last_activity = _utc_now()
                state.condition.notify_all()

    def start(self, container: Any) -> bool:
        return self.start_sandbox(container)

    def stop(self, container: Any) -> bool:
        return self.stop_sandbox(container)

    def kill(self, container: Any) -> bool:
        return self.kill_sandbox(container)

    def claim_for_removal(
        self,
        container_id: str,
        idle_before: datetime | None = None,
        observed_last_activity: datetime | None = None,
    ) -> bool:
        state = self._state_for(container_id, create=False)
        if state is None:
            state = self._state_for(container_id)
        assert state is not None
        with state.condition:
            if state.recovering or state.active_count:
                return False
            observed = _as_utc(observed_last_activity) if observed_last_activity else None
            if observed and observed > state.last_activity:
                state.last_activity = observed
            if idle_before is not None and state.last_activity >= _as_utc(idle_before):
                return False
            state.recovering = True
            state.available = False
            state.condition.notify_all()
            return True

    def remove_sandbox(self, container_id: str, *, force: bool = False) -> bool:
        try:
            container = self.get_sandbox(container_id)
        except Exception as exc:
            if self._is_not_found(exc):
                self._states.pop(container_id, None)
                return True
            raise

        state = self._state_for(container_id)
        assert state is not None
        claimed = False
        try:
            with state.condition:
                if not state.recovering:
                    if state.active_count and not force:
                        return False
                    state.recovering = True
                    state.available = False
                claimed = True

            labels = dict(
                getattr(container, "labels", None)
                or (getattr(container, "attrs", {}) or {}).get("Config", {}).get("Labels", {})
                or {}
            )
            token = labels.get(CONTAINER_TOKEN_LABEL, "")
            network_names = self._container_network_names(container)
            if not force and self._container_running(container):
                if not self._stop_or_kill(container):
                    raise RuntimeError("Docker sandbox could not stop before removal")
            container.remove(force=force)
            self._remove_container_networks(network_names, token)
            with self._state_lock:
                self._states.pop(container_id, None)
            return True
        except Exception as exc:
            with state.condition:
                state.recovering = False
                state.available = True
                state.condition.notify_all()
            if self._is_not_found(exc):
                with self._state_lock:
                    self._states.pop(container_id, None)
                return True
            if not claimed:
                return False
            raise

    def remove(self, container_id: str, *, force: bool = False) -> bool:
        return self.remove_sandbox(container_id, force=force)

    def cleanup_orphan_networks(self, older_than: datetime) -> int:
        removed = 0
        with self._creation_lock:
            networks = self.management_client.networks.list(
                filters={"label": self._labels_for_filter()}
            )
            for network in networks:
                reload = getattr(network, "reload", None)
                if callable(reload):
                    try:
                        reload()
                    except Exception as exc:
                        if self._is_not_found(exc):
                            continue
                        raise
                attrs = dict(getattr(network, "attrs", {}) or {})
                labels = dict(getattr(network, "labels", None) or attrs.get("Labels", {}) or {})
                created = _parse_iso(labels.get(CREATED_AT_LABEL))
                if (
                    labels.get(MANAGED_LABEL) != "true"
                    or labels.get(PLATFORM_LABEL) != "docker"
                    or labels.get(NAMESPACE_LABEL) != self.namespace
                    or not created
                    or created >= _as_utc(older_than)
                ):
                    continue
                containers = attrs.get("Containers") or {}
                if containers:
                    continue
                try:
                    network.remove()
                except Exception as exc:
                    if not self._is_not_found(exc):
                        logger.warning("Failed to remove orphan Docker network: %s", exc)
                else:
                    removed += 1
        return removed


__all__ = [
    "CONFIG_HASH_LABEL",
    "CONTAINER_PREFIX",
    "CONTAINER_TOKEN_LABEL",
    "CREATED_AT_LABEL",
    "DockerSandboxAdapter",
    "MANAGED_LABEL",
    "NAMESPACE_LABEL",
    "NETWORK_PREFIX",
    "OWNER_HASH_LABEL",
    "PLATFORM_LABEL",
    "_OperationState",
    "container_config_hash",
]

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from dataclasses import replace

import pytest

from src.infra.sandbox._docker_adapter import DockerSandboxAdapter, container_config_hash
from src.infra.sandbox.base import DockerSandboxConfig
from src.kernel.config.docker_sandbox import DOCKER_SANDBOX_CONTRACT_VERSION


def _config(**overrides: object) -> DockerSandboxConfig:
    config = DockerSandboxConfig()
    return replace(config, **overrides)


def test_docker_adapter_rejects_unauthenticated_tcp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)

    with pytest.raises(
        ValueError, match="Docker sandbox refuses an unauthenticated TCP Docker endpoint"
    ):
        DockerSandboxAdapter(_config())


def test_container_config_hash_includes_contract_version_and_creation_limits() -> None:
    base = _config()
    assert container_config_hash(base) == container_config_hash(_config())
    assert container_config_hash(base) != container_config_hash(
        _config(memory_limit_mb=base.memory_limit_mb + 1)
    )
    assert DOCKER_SANDBOX_CONTRACT_VERSION == "1"


def test_adapter_initializes_three_clients_and_fails_closed_on_missing_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _Client:
        def ping(self) -> bool:
            return True

        def info(self) -> dict[str, object]:
            return {
                "OSType": "linux",
                "MemoryLimit": True,
                "SwapLimit": True,
                "CpuCfsQuota": True,
                "PidsLimit": True,
                "SecurityOptions": ["name=seccomp"],
            }

        def close(self) -> None:
            return None

    def from_env(**kwargs: object) -> _Client:
        calls.append(kwargs)
        return _Client()

    fake_docker = types.SimpleNamespace(from_env=from_env)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)
    adapter = DockerSandboxAdapter(_config(timeout=200))

    adapter.management_client

    assert [call["timeout"] for call in calls] == [120, 215, 15]
    assert all(call["version"] == "auto" for call in calls)

    class _BadClient(_Client):
        def info(self) -> dict[str, object]:
            return {"OSType": "linux", "SecurityOptions": []}

    monkeypatch.setattr(adapter, "_new_client", lambda _timeout: _BadClient())
    adapter.close()
    with pytest.raises(RuntimeError, match="required Linux isolation capabilities"):
        adapter.management_client


@contextmanager
def _operation(_container: object):
    yield None


def test_adapter_container_defaults_are_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DockerSandboxAdapter(_config())
    captured: dict[str, object] = {}
    fake_types = types.SimpleNamespace(LogConfig=lambda **kwargs: kwargs)
    monkeypatch.setattr(adapter, "_docker", types.SimpleNamespace(types=fake_types))
    network = types.SimpleNamespace(name="private-net")

    captured = adapter._container_kwargs(
        name="lambchat-sbx-default-owner-token",
        labels={"io.lambchat.sandbox.managed": "true"},
        network=network,
    )

    assert captured["user"] == "65534:65534"
    assert captured["cap_drop"] == ["ALL"]
    assert captured["security_opt"] == ["no-new-privileges:true"]
    assert captured["network"] == "private-net"
    assert "privileged" not in captured
    assert "volumes" not in captured
    assert "ports" not in captured
    assert "devices" not in captured


def test_operation_state_lookup_does_not_create_state() -> None:
    adapter = DockerSandboxAdapter(_config())
    assert adapter.get_operation_state("unknown") is None

    container = types.SimpleNamespace(id="container-1")
    with adapter.operation(container) as state:
        assert adapter.get_operation_state("container-1") is state
        assert state.active_count == 1
    assert state.active_count == 0


def test_count_sandboxes_uses_namespace_filtered_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DockerSandboxAdapter(_config())
    monkeypatch.setattr(
        adapter,
        "list_sandboxes",
        lambda *, include_stopped=True: [object(), object()] if include_stopped else [object()],
    )

    assert adapter.count_sandboxes() == 2
    assert adapter.count_sandboxes(include_stopped=False) == 1

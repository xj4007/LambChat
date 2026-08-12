from __future__ import annotations

import pytest

from src.infra.sandbox.base import (
    SandboxFactory,
    _SandboxRegistration,
    get_sandbox_config_from_settings,
)


@pytest.fixture(autouse=True)
def _clear_sandbox_factory_registry() -> None:
    SandboxFactory._sandbox_registry.clear()
    SandboxFactory._run_id_to_sandbox.clear()


@pytest.mark.asyncio
async def test_close_sandbox_offloads_provider_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside_blocking_io = False

    class _DaytonaProvider:
        __module__ = "daytona.fake"

        def __init__(self) -> None:
            self.deleted = False

        def delete(self) -> None:
            assert inside_blocking_io, "provider delete must be offloaded"
            self.deleted = True

    provider = _DaytonaProvider()
    SandboxFactory._sandbox_registry["sandbox-1"] = _SandboxRegistration(
        backend=object(),
        provider=provider,
        close=provider.delete,
    )

    async def _fake_run_blocking_io(func, /, *args, **kwargs):
        nonlocal inside_blocking_io
        assert inside_blocking_io is False
        inside_blocking_io = True
        try:
            return func(*args, **kwargs)
        finally:
            inside_blocking_io = False

    monkeypatch.setattr(
        "src.infra.sandbox.base.run_blocking_io",
        _fake_run_blocking_io,
        raising=False,
    )

    closed = await SandboxFactory.close_sandbox("sandbox-1")

    assert closed is True
    assert provider.deleted is True
    assert "sandbox-1" not in SandboxFactory._sandbox_registry


@pytest.mark.asyncio
async def test_close_sandbox_treats_missing_provider_as_success() -> None:
    class _MissingProvider:
        def delete(self) -> None:
            raise RuntimeError("container not found")

    SandboxFactory._sandbox_registry["missing"] = _SandboxRegistration(
        backend=object(),
        provider=_MissingProvider(),
        close=lambda: _MissingProvider().delete(),
    )

    assert await SandboxFactory.close_sandbox("missing") is True
    assert "missing" not in SandboxFactory._sandbox_registry


def test_cubesandbox_config_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.infra.sandbox.base.settings.SANDBOX_PLATFORM", "cubesandbox")
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_API_URL", "http://127.0.0.1:13000")
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_TEMPLATE", "tpl-cube")
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_PROXY_NODE_IP", "127.0.0.1")
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_PROXY_PORT_HTTP", 11080)
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_SANDBOX_DOMAIN", "cube.app")
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_TIMEOUT", 7200)
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_REQUEST_TIMEOUT", 180)
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_AUTO_PAUSE", True)
    monkeypatch.setattr("src.infra.sandbox.base.settings.CUBE_AUTO_RESUME", True)

    config = get_sandbox_config_from_settings()

    assert config.platform == "cubesandbox"
    assert config.api_url == "http://127.0.0.1:13000"
    assert config.template == "tpl-cube"
    assert config.proxy_node_ip == "127.0.0.1"
    assert config.proxy_port_http == 11080
    assert config.sandbox_domain == "cube.app"
    assert config.timeout == 7200
    assert config.request_timeout == 180
    assert config.auto_pause is True
    assert config.auto_resume is True


def test_docker_config_from_settings_uses_all_sandbox_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.infra.sandbox.base.settings.SANDBOX_PLATFORM", "docker")
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_NAMESPACE", "test-ns")
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_IMAGE", "python:test")
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_TIMEOUT", 12)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_IDLE_TIMEOUT", 60)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_CLEANUP_INTERVAL", 10)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_MAX_CONTAINERS", 3)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_MEMORY_LIMIT_MB", 256)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_CPU_LIMIT", 0.5)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_PIDS_LIMIT", 32)
    monkeypatch.setattr("src.infra.sandbox.base.settings.DOCKER_SANDBOX_NETWORK_MODE", "none")
    monkeypatch.setattr(
        "src.infra.sandbox.base.settings.DOCKER_SANDBOX_MAX_OUTPUT_BYTES", 1024 * 1024
    )

    config = get_sandbox_config_from_settings()

    assert config.platform == "docker"
    assert config.namespace == "test-ns"
    assert config.image == "python:test"
    assert config.timeout == 12
    assert config.idle_timeout == 60
    assert config.cleanup_interval == 10
    assert config.max_containers == 3
    assert config.memory_limit_mb == 256
    assert config.cpu_limit == 0.5
    assert config.pids_limit == 32
    assert config.network_mode == "none"
    assert config.max_output_bytes == 1024 * 1024

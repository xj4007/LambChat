from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import datetime, timezone

import pytest

from src.infra.backend.docker import DockerSandboxBackend
from src.infra.sandbox._docker_adapter import DockerSandboxAdapter
from src.infra.sandbox.base import DockerSandboxConfig


def test_docker_sandbox_engine_contract_end_to_end() -> None:
    if os.environ.get("RUN_DOCKER_SANDBOX_INTEGRATION") != "1":
        pytest.skip("set RUN_DOCKER_SANDBOX_INTEGRATION=1 to run Docker integration tests")

    namespace = f"docker-itest-{uuid.uuid4().hex[:12]}"
    config = DockerSandboxConfig(
        namespace=namespace,
        image="python:3.12-slim-bookworm",
        timeout=30,
        idle_timeout=60,
        cleanup_interval=10,
        max_containers=4,
        memory_limit_mb=512,
        cpu_limit=1.0,
        pids_limit=128,
        max_output_bytes=1024 * 1024,
    )
    adapter = DockerSandboxAdapter(config)
    try:
        user_a = adapter.create_sandbox("integration-user-a")
        user_b = adapter.create_sandbox("integration-user-b")
        assert adapter.start_sandbox(user_a)
        assert adapter.start_sandbox(user_b)

        session_a = DockerSandboxBackend(
            user_a,
            adapter,
            timeout=config.timeout,
            max_output_bytes=config.max_output_bytes,
            work_dir="/tmp/lambchat-workspace/sessions/session-a",
        )
        session_a_other = DockerSandboxBackend(
            user_a,
            adapter,
            timeout=config.timeout,
            max_output_bytes=config.max_output_bytes,
            work_dir="/tmp/lambchat-workspace/sessions/session-a-other",
        )
        session_b = DockerSandboxBackend(
            user_b,
            adapter,
            timeout=config.timeout,
            max_output_bytes=config.max_output_bytes,
            work_dir="/tmp/lambchat-workspace/sessions/session-b",
        )

        first = session_a.execute("printf docker-ok")
        assert first.exit_code == 0
        assert first.output == "docker-ok"
        write_result = session_a.write(
            "/tmp/lambchat-workspace/sessions/session-a/file.txt",
            "persisted",
        )
        assert write_result.error is None
        assert (
            session_a.execute("mkdir -p /tmp/lambchat-workspace/sessions/session-a-other").exit_code
            == 0
        )
        assert (
            session_b.execute("mkdir -p /tmp/lambchat-workspace/sessions/session-b").exit_code == 0
        )
        assert session_a_other.execute("test ! -e file.txt").exit_code == 0
        assert (
            session_b.execute(
                "test ! -e /tmp/lambchat-workspace/sessions/session-a/file.txt"
            ).exit_code
            == 0
        )

        offloaded = session_a.execute_with_offload(
            "python3 -c \"print('x' * 256)\"",
            "/tmp/lambchat-workspace/sessions/session-a/capture.txt",
            max_inline_bytes=16,
            max_capture_bytes=4096,
        )
        assert offloaded.offloaded is True
        assert (
            "x" * 256
            in session_a.execute(
                "cat /tmp/lambchat-workspace/sessions/session-a/capture.txt"
            ).output
        )

        async def cancel_long_command() -> None:
            task = asyncio.create_task(session_a.aexecute("sleep 30"))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_long_command())
        assert adapter._container_running(user_a) is False
        assert adapter.start_sandbox(user_a)
        assert adapter.stop_sandbox(user_a)
        assert adapter.start_sandbox(user_a)
        assert (
            session_a.execute("cat /tmp/lambchat-workspace/sessions/session-a/file.txt").output
            == "persisted"
        )

        user_a.reload()
        attrs = user_a.attrs
        host_config = attrs.get("HostConfig", {})
        config_attrs = attrs.get("Config", {})
        assert str(config_attrs.get("User")) == "65534:65534"
        assert "ALL" in host_config.get("CapDrop", [])
        assert "no-new-privileges:true" in host_config.get("SecurityOpt", [])
        assert not attrs.get("Mounts")
        assert not host_config.get("PortBindings")
        assert host_config.get("NetworkMode") != "host"
    finally:
        try:
            containers = adapter.list_sandboxes()
        except Exception:
            containers = []
        for container in containers:
            with contextlib.suppress(Exception):
                adapter.remove_sandbox(adapter.get_sandbox_id(container), force=True)
        with contextlib.suppress(Exception):
            adapter.cleanup_orphan_networks(datetime.max.replace(tzinfo=timezone.utc))
        adapter.close()

from pathlib import Path

import yaml


def test_compose_passes_e2b_settings_without_committed_secret() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load(
        (repository_root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["lambchat"]["environment"]

    assert "E2B_API_KEY=${E2B_API_KEY:-}" in environment
    assert "E2B_TEMPLATE=${E2B_TEMPLATE:-base}" in environment
    assert "ENABLE_SANDBOX=${ENABLE_SANDBOX:-false}" in environment
    assert "SANDBOX_PLATFORM=${SANDBOX_PLATFORM:-daytona}" in environment
    assert "DOCKER_SANDBOX_NAMESPACE=${DOCKER_SANDBOX_NAMESPACE:-default}" in environment
    assert "DOCKER_SANDBOX_NETWORK_MODE=${DOCKER_SANDBOX_NETWORK_MODE:-bridge}" in environment

    example = (repository_root / "deploy/.env.example").read_text(encoding="utf-8")
    assert "E2B_API_KEY=" in example
    assert "E2B_TEMPLATE=base" in example
    assert "e2b_" not in example


def test_deploy_env_example_declares_required_runtime_secrets() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    example = (repository_root / "deploy/.env.example").read_text(encoding="utf-8")

    assert "JWT_SECRET_KEY=" in example
    assert "MCP_ENCRYPTION_SALT=" in example


def test_deploy_script_prepares_non_root_mounts_before_start() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = (repository_root / "deploy/deploy.sh").read_text(encoding="utf-8")

    permission_setup = "docker compose run --rm --no-deps --user root"
    assert permission_setup in script
    assert script.index(permission_setup) < script.index("docker compose up -d")


def test_base_compose_is_socket_free_and_not_privileged() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    compose_path = repository_root / "deploy/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    raw = compose_path.read_text(encoding="utf-8")

    assert "/var/run/docker.sock" not in raw
    assert "tcp://" not in raw
    assert "2375" not in raw
    assert "privileged" not in raw
    assert compose["services"]["lambchat"].get("user") != "root"


def test_docker_override_only_grants_socket_to_non_root_lambchat() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    override = yaml.safe_load(
        (repository_root / "deploy/docker-compose.docker-sandbox.yml").read_text(encoding="utf-8")
    )

    assert set(override["services"]) == {"lambchat"}
    service = override["services"]["lambchat"]
    assert service["user"] == "app"
    assert service["environment"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert service["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]
    assert service["group_add"] == ["${DOCKER_GID:?set DOCKER_GID to the host Docker socket group}"]
    assert "privileged" not in service


def test_deploy_env_example_has_no_endpoint_credentials() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    example = (repository_root / "deploy/.env.example").read_text(encoding="utf-8")

    assert example.count("/var/run/docker.sock") == 1
    assert "DOCKER_HOST=" not in example
    assert "tcp://" not in example
    assert "DOCKER_TLS_VERIFY=" not in example
    assert "DOCKER_CERT_PATH=" not in example
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.docker-sandbox.yml" in example

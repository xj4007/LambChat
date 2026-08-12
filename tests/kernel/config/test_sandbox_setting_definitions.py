from __future__ import annotations

import pytest

from src.kernel.config import RESTART_REQUIRED_SETTINGS, SETTING_DEFINITIONS
from src.kernel.config.docker_sandbox import (
    DOCKER_SANDBOX_DEFAULTS,
    DOCKER_SANDBOX_OPTIONS,
    DOCKER_SANDBOX_RANGES,
    validate_docker_sandbox_value,
)


def test_sandbox_platform_options_keep_existing_order_and_add_docker() -> None:
    definition = SETTING_DEFINITIONS["SANDBOX_PLATFORM"]

    assert definition["options"] == ["daytona", "e2b", "cubesandbox", "docker"]
    assert definition["default"] == "daytona"
    assert definition["depends_on"] == "ENABLE_SANDBOX"


def test_docker_settings_expose_shared_defaults_and_restart_contract() -> None:
    for key, default in DOCKER_SANDBOX_DEFAULTS.items():
        definition = SETTING_DEFINITIONS[key]
        assert definition["default"] == default
        assert definition["depends_on"] == {"key": "SANDBOX_PLATFORM", "value": "docker"}

    assert SETTING_DEFINITIONS["DOCKER_SANDBOX_NETWORK_MODE"]["options"] == ["bridge", "none"]
    assert "SANDBOX_PLATFORM" in RESTART_REQUIRED_SETTINGS
    assert "DOCKER_SANDBOX_NAMESPACE" in RESTART_REQUIRED_SETTINGS


def test_docker_validation_rejects_boolean_numeric_values() -> None:
    with pytest.raises(ValueError, match="DOCKER_SANDBOX_MAX_CONTAINERS"):
        validate_docker_sandbox_value("DOCKER_SANDBOX_MAX_CONTAINERS", True)


def test_docker_validation_accepts_default_contract() -> None:
    for key, value in DOCKER_SANDBOX_DEFAULTS.items():
        validate_docker_sandbox_value(key, value)

    assert DOCKER_SANDBOX_OPTIONS["DOCKER_SANDBOX_NETWORK_MODE"] == ["bridge", "none"]
    assert DOCKER_SANDBOX_RANGES["DOCKER_SANDBOX_TIMEOUT"] == (1, 86400)

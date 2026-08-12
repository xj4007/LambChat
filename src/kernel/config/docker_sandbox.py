"""Shared Docker sandbox defaults and validation rules.

This module intentionally has no Docker SDK dependency.  Configuration metadata,
Pydantic settings, database writes, and runtime mapping all use these rules so
invalid values cannot be accepted through one configuration path and rejected by
another.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

DOCKER_SANDBOX_CONTRACT_VERSION = "1"

DOCKER_SANDBOX_DEFAULTS: dict[str, Any] = {
    "DOCKER_SANDBOX_NAMESPACE": "default",
    "DOCKER_SANDBOX_IMAGE": "python:3.12-slim-bookworm",
    "DOCKER_SANDBOX_TIMEOUT": 180,
    "DOCKER_SANDBOX_IDLE_TIMEOUT": 1800,
    "DOCKER_SANDBOX_CLEANUP_INTERVAL": 60,
    "DOCKER_SANDBOX_MAX_CONTAINERS": 20,
    "DOCKER_SANDBOX_MEMORY_LIMIT_MB": 1024,
    "DOCKER_SANDBOX_CPU_LIMIT": 1.0,
    "DOCKER_SANDBOX_PIDS_LIMIT": 256,
    "DOCKER_SANDBOX_NETWORK_MODE": "bridge",
    "DOCKER_SANDBOX_MAX_OUTPUT_BYTES": 10 * 1024 * 1024,
}

DOCKER_SANDBOX_KEYS = tuple(DOCKER_SANDBOX_DEFAULTS)
DOCKER_SANDBOX_OPTIONS: dict[str, list[str]] = {
    "DOCKER_SANDBOX_NETWORK_MODE": ["bridge", "none"],
}

# Inclusive numeric bounds.  CPU is deliberately separate because it accepts
# finite ints/floats while every other numeric Docker setting is an int only.
DOCKER_SANDBOX_RANGES: dict[str, tuple[int | float, int | float]] = {
    "DOCKER_SANDBOX_TIMEOUT": (1, 86400),
    "DOCKER_SANDBOX_IDLE_TIMEOUT": (60, 604800),
    "DOCKER_SANDBOX_CLEANUP_INTERVAL": (10, 3600),
    "DOCKER_SANDBOX_MAX_CONTAINERS": (1, 100),
    "DOCKER_SANDBOX_MEMORY_LIMIT_MB": (128, 32768),
    "DOCKER_SANDBOX_CPU_LIMIT": (0.1, 64),
    "DOCKER_SANDBOX_PIDS_LIMIT": (16, 4096),
    "DOCKER_SANDBOX_MAX_OUTPUT_BYTES": (1024 * 1024, 100 * 1024 * 1024),
}

_DOCKER_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}\Z")


def _raise_invalid(key: str, reason: str) -> None:
    raise ValueError(f"Setting {key} {reason}")


def validate_docker_sandbox_value(key: str, value: Any) -> None:
    """Validate one Docker sandbox setting without coercing or clamping it."""

    if key not in DOCKER_SANDBOX_DEFAULTS:
        raise ValueError(f"Unknown Docker sandbox setting {key}")

    if key == "DOCKER_SANDBOX_NAMESPACE":
        if not isinstance(value, str) or _DOCKER_NAMESPACE_RE.fullmatch(value) is None:
            _raise_invalid(key, "must match [a-z0-9][a-z0-9_.-]{0,31}")
        return

    if key == "DOCKER_SANDBOX_IMAGE":
        if not isinstance(value, str) or not value or value != value.strip():
            _raise_invalid(key, "must be a non-empty string without surrounding whitespace")
        return

    if key == "DOCKER_SANDBOX_NETWORK_MODE":
        if value not in DOCKER_SANDBOX_OPTIONS[key]:
            _raise_invalid(key, f"expects one of: {DOCKER_SANDBOX_OPTIONS[key]}")
        return

    bounds = DOCKER_SANDBOX_RANGES[key]
    if key == "DOCKER_SANDBOX_CPU_LIMIT":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            _raise_invalid(key, "must be a finite number")
    elif isinstance(value, bool) or not isinstance(value, int):
        _raise_invalid(key, "must be an integer")

    lower, upper = bounds
    if value < lower or value > upper:
        _raise_invalid(key, f"must be between {lower} and {upper}")


def validate_docker_sandbox_values(values: Mapping[str, Any]) -> None:
    """Validate every Docker setting present in a mapping."""

    for key in DOCKER_SANDBOX_KEYS:
        if key in values:
            validate_docker_sandbox_value(key, values[key])


__all__ = [
    "DOCKER_SANDBOX_CONTRACT_VERSION",
    "DOCKER_SANDBOX_DEFAULTS",
    "DOCKER_SANDBOX_KEYS",
    "DOCKER_SANDBOX_OPTIONS",
    "DOCKER_SANDBOX_RANGES",
    "validate_docker_sandbox_value",
    "validate_docker_sandbox_values",
]

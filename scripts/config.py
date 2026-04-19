"""Config loader — YAML files under config/, cached per process."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def config_dir() -> Path:
    override = os.environ.get("CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=16)
def load(name: str) -> dict[str, Any]:
    """Load config/<name>.yml. Cached."""
    path = config_dir() / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def fundednext() -> dict[str, Any]:
    return load("fundednext")


def instruments() -> dict[str, Any]:
    return load("instruments")["instruments"]


def sessions() -> dict[str, Any]:
    return load("sessions")["sessions"]


def clear_cache() -> None:
    """For tests — drop memoized configs."""
    load.cache_clear()

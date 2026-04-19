"""Test fixtures — isolate memory dir per test and force MockBroker."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import config
from scripts.broker.mock_broker import MockBroker


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    memdir = tmp_path / "memory"
    (memdir / "daily-journal").mkdir(parents=True)
    (memdir / "weekly-reviews").mkdir(parents=True)
    monkeypatch.setenv("MEMORY_DIR", str(memdir))
    monkeypatch.setenv("BROKER_MODE", "mock")
    monkeypatch.setenv("TRADING_STAGE", "dev")
    monkeypatch.setenv("INITIAL_BALANCE", "50000")
    # Keep discord webhook unset → notify falls back to print
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    config.clear_cache()
    return memdir


@pytest.fixture
def mock_broker() -> MockBroker:
    b = MockBroker(initial_balance=50000.0)
    b.connect()
    return b


@pytest.fixture
def tz_utc():
    """Return a UTC `now` anchored to a Wednesday 10:00 UTC — mid-week, open sessions."""
    from datetime import UTC, datetime
    return datetime(2026, 4, 22, 10, 0, tzinfo=UTC)  # Wednesday


@pytest.fixture
def tz_friday_flatten():
    """Friday 21:30 UTC — past flatten window."""
    from datetime import UTC, datetime
    return datetime(2026, 4, 24, 21, 30, tzinfo=UTC)

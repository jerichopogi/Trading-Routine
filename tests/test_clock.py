"""Display-timezone helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts import clock


def test_default_display_tz_is_new_york(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY_TIMEZONE", raising=False)
    tz = clock.display_tz()
    assert str(tz) == "America/New_York"


def test_display_tz_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Manila")
    assert str(clock.display_tz()) == "Asia/Manila"


def test_to_display_converts_utc_to_et(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    # 2026-07-15 13:30 UTC → 09:30 EDT (summer, UTC-4)
    utc_dt = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    display = clock.to_display(utc_dt)
    assert display.hour == 9
    assert display.minute == 30


def test_to_display_handles_naive_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    naive = datetime(2026, 7, 15, 13, 30)
    display = clock.to_display(naive)
    assert display.hour == 9


def test_trading_date_in_ny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    # UTC midnight on 2026-04-22 is 2026-04-21 20:00 EDT → NY date is still 21st.
    utc_dt = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    assert clock.trading_date(utc_dt).isoformat() == "2026-04-21"


def test_trading_date_in_manila(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Manila")
    # UTC 2026-04-22 00:00 is 08:00 PHT → still the 22nd
    utc_dt = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    assert clock.trading_date(utc_dt).isoformat() == "2026-04-22"


def test_format_display_includes_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    utc_dt = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    s = clock.format_display(utc_dt)
    assert "09:30" in s
    assert "EDT" in s or "EST" in s


def test_journal_filename_uses_trading_date(
    monkeypatch: pytest.MonkeyPatch, isolated_memory
) -> None:
    """NY trading date drives the daily journal filename."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    from scripts import journal
    path = journal.append_daily_note("smoke", "hello")
    # Filename should be a date like YYYY-MM-DD.md
    assert path.name.endswith(".md")
    assert path.parent.name == "daily-journal"
    assert isolated_memory.exists()

"""Display-timezone helpers.

UTC is the storage timezone (timestamps in JSONL, configs, DD math). This
module handles the *display* timezone — what humans see in Discord pings,
daily journal filenames, and CLI output.

Default display tz is America/New_York (matches the FX/index trader's
"trading day"). Override with DISPLAY_TIMEZONE env var (IANA name).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

DEFAULT_DISPLAY_TZ = "America/New_York"


def display_tz() -> ZoneInfo:
    name = os.environ.get("DISPLAY_TIMEZONE", DEFAULT_DISPLAY_TZ).strip() or DEFAULT_DISPLAY_TZ
    return ZoneInfo(name)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_display() -> datetime:
    return datetime.now(display_tz())


def to_display(dt: datetime) -> datetime:
    """Convert a UTC (or aware) datetime to display tz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(display_tz())


def format_display(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    return to_display(dt).strftime(fmt)


def trading_date(dt: datetime | None = None) -> date:
    """The calendar date in the display timezone — used for daily journal filenames."""
    dt = dt or now_utc()
    return to_display(dt).date()


def trading_iso_week(dt: datetime | None = None) -> tuple[int, int]:
    """ISO year + week in the display timezone — used for weekly review filenames."""
    dt = dt or now_utc()
    iso = to_display(dt).isocalendar()
    return iso[0], iso[1]

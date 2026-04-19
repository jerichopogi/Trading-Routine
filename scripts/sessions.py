"""Market session clock.

Answers:
- Is instrument X currently within its trading session?
- Is it within the Friday flatten window?
- Is the session about to close (for last-trade cutoffs)?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from .config import fundednext, instruments, sessions


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m), tzinfo=UTC)


def _now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SessionStatus:
    symbol: str
    session: str
    open_now: bool
    minutes_to_close: int | None  # None if closed; 0 if closed at "close" boundary


def session_of(symbol: str) -> str:
    inst = instruments().get(symbol)
    if inst is None:
        raise KeyError(f"Unknown instrument {symbol!r}")
    return inst["session"]


def is_open(symbol: str, now: datetime | None = None) -> bool:
    return status(symbol, now).open_now


def status(symbol: str, now: datetime | None = None) -> SessionStatus:
    now_utc = _now_utc(now)
    session_name = session_of(symbol)
    segments = sessions()[session_name]["segments"]

    day = now_utc.weekday()  # Mon=0 .. Sun=6
    now_t = now_utc.timetz()

    open_now = False
    minutes_to_close: int | None = None

    for seg in segments:
        if day not in seg["days"]:
            continue
        open_t = _parse_hhmm(seg["open"])
        close_t = _parse_hhmm(seg["close"])
        if open_t <= now_t <= close_t:
            open_now = True
            minutes_to_close = int(
                (datetime.combine(now_utc.date(), close_t) - datetime.combine(now_utc.date(), now_t)).total_seconds() // 60
            )
            break

    return SessionStatus(
        symbol=symbol,
        session=session_name,
        open_now=open_now,
        minutes_to_close=minutes_to_close,
    )


def is_weekend_flatten_window(now: datetime | None = None) -> bool:
    """True on Friday at or after the configured flatten time (UTC)."""
    now_utc = _now_utc(now)
    cfg = fundednext()["sessions"]
    flatten_t = _parse_hhmm(cfg["flatten_friday_utc"])
    return now_utc.weekday() == 4 and now_utc.timetz() >= flatten_t


def should_avoid_new_trades(now: datetime | None = None) -> tuple[bool, str]:
    """
    Returns (avoid, reason). Called by guardrails before allowing new positions.
    """
    now_utc = _now_utc(now)
    cfg = fundednext()["sessions"]
    flatten_t = _parse_hhmm(cfg["flatten_friday_utc"])

    # No new trades on Friday within the final 2 hours before flatten.
    if now_utc.weekday() == 4:
        close_dt = datetime.combine(now_utc.date(), flatten_t)
        now_naive = datetime.combine(now_utc.date(), now_utc.timetz())
        if now_naive >= close_dt - timedelta(hours=2):
            return True, "Within 2h of Friday flatten — no new positions"

    if is_weekend_flatten_window(now_utc):
        return True, "Weekend — markets flattened"

    return False, ""

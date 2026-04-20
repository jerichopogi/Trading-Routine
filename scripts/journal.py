"""Memory I/O — trade log, daily journal, weekly reviews.

All writes are append-only JSONL or additive markdown. The agent mutates
markdown freely; structured logs are never overwritten.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from . import clock
from .broker import OrderRequest, OrderResult, Position


def memory_dir() -> Path:
    override = os.environ.get("MEMORY_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "memory"


def _trade_log_path() -> Path:
    return memory_dir() / "trade-log.jsonl"


def _daily_journal_path(d: date | None = None) -> Path:
    # Use display-timezone "trading date" so the journal file matches the
    # user's mental calendar (NY date by default).
    d = d or clock.trading_date()
    return memory_dir() / "daily-journal" / f"{d.isoformat()}.md"


def _weekly_review_path(iso_year: int | None = None, iso_week: int | None = None) -> Path:
    if iso_year and iso_week:
        y, w = iso_year, iso_week
    else:
        y, w = clock.trading_iso_week()
    return memory_dir() / "weekly-reviews" / f"{y}-W{w:02d}.md"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=_json_default) + "\n")


def _json_default(obj: object) -> object:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):   # Enum
        return obj.value
    raise TypeError(f"cannot serialize {type(obj)}")


# ----- trade log events -----

def log_order(order: OrderRequest, result: OrderResult, stage: str) -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "order",
        "ts": datetime.now(UTC).isoformat(),
        "stage": stage,
        "order": order,
        "result": result,
    })


def log_rejection(order: OrderRequest, verdict: object, stage: str) -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "rejection",
        "ts": datetime.now(UTC).isoformat(),
        "stage": stage,
        "order": order,
        "verdict": verdict,
    })


def log_modify(ticket: int, new_sl: float | None = None, new_tp: float | None = None,
               reason: str = "") -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "modify",
        "ts": datetime.now(UTC).isoformat(),
        "ticket": ticket,
        "new_sl": new_sl,
        "new_tp": new_tp,
        "reason": reason,
    })


def log_close(position: Position, ok: bool, reason: str, stage: str) -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "close",
        "ts": datetime.now(UTC).isoformat(),
        "stage": stage,
        "ok": ok,
        "reason": reason,
        "position": position,
    })


def log_cancel_pending(ticket: int, symbol: str, reason: str) -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "cancel_pending",
        "ts": datetime.now(UTC).isoformat(),
        "ticket": ticket,
        "symbol": symbol,
        "reason": reason,
    })


def log_partial_close(
    ticket: int, symbol: str, volume_closed: float, realized_r: float | None,
    remaining_volume: float, reason: str,
) -> None:
    _append_jsonl(_trade_log_path(), {
        "event": "partial_close",
        "ts": datetime.now(UTC).isoformat(),
        "ticket": ticket,
        "symbol": symbol,
        "volume_closed": volume_closed,
        "realized_r": realized_r,
        "remaining_volume": remaining_volume,
        "reason": reason,
    })


# ----- human-readable markdown journals -----

def append_daily_note(section: str, body: str, d: date | None = None) -> Path:
    """Append a section to today's daily journal. Creates the file if missing.

    Uses display-tz date (NY by default) for filename and header; each block
    records both UTC and display-tz timestamps so history is unambiguous.
    """
    path = _daily_journal_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    trading_day = (d or clock.trading_date()).isoformat()
    header = f"# {trading_day}\n\n" if not path.exists() else ""
    now_utc = datetime.now(UTC)
    ts = f"{now_utc.isoformat(timespec='seconds')} ({clock.format_display(now_utc, '%H:%M %Z')})"
    block = f"\n## {section}\n_{ts}_\n\n{body}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(header + block)
    return path


def append_weekly_note(section: str, body: str) -> Path:
    path = _weekly_review_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    y, w = clock.trading_iso_week()
    header = f"# {y}-W{w:02d}\n\n" if not path.exists() else ""
    now_utc = datetime.now(UTC)
    ts = f"{now_utc.isoformat(timespec='seconds')} ({clock.format_display(now_utc, '%H:%M %Z')})"
    block = f"\n## {section}\n_{ts}_\n\n{body}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(header + block)
    return path


def read_strategy() -> str:
    p = memory_dir() / "strategy.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_playbook() -> str:
    p = memory_dir() / "playbook.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""

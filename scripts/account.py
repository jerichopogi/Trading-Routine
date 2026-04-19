"""Account state tracking: equity curve, drawdown math, rule violations.

The equity curve file (memory/equity-curve.jsonl) is the source of truth for
all drawdown calculations — NOT the broker's reported values, because we need
historical state to detect daily loss resets and peak drawdown.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from .broker import AccountInfo, Broker
from .config import fundednext


@dataclass(frozen=True)
class EquitySnapshot:
    ts: datetime
    balance: float
    equity: float
    open_profit: float
    stage: str
    note: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "ts": self.ts.isoformat(),
            "balance": self.balance,
            "equity": self.equity,
            "open_profit": self.open_profit,
            "stage": self.stage,
            "note": self.note,
        })

    @staticmethod
    def from_json(line: str) -> EquitySnapshot:
        d = json.loads(line)
        return EquitySnapshot(
            ts=datetime.fromisoformat(d["ts"]),
            balance=float(d["balance"]),
            equity=float(d["equity"]),
            open_profit=float(d["open_profit"]),
            stage=d.get("stage", "dev"),
            note=d.get("note", ""),
        )


@dataclass(frozen=True)
class RuleStatus:
    daily_dd_pct: float
    max_dd_pct: float
    daily_dd_tripped: bool
    max_dd_tripped: bool
    hard_stop_daily_tripped: bool
    hard_stop_max_tripped: bool
    reasons: list[str]

    @property
    def any_violation(self) -> bool:
        return self.hard_stop_daily_tripped or self.hard_stop_max_tripped

    @property
    def any_firm_violation(self) -> bool:
        return self.daily_dd_tripped or self.max_dd_tripped


def memory_dir() -> Path:
    override = os.environ.get("MEMORY_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "memory"


def equity_curve_path() -> Path:
    return memory_dir() / "equity-curve.jsonl"


def read_equity_curve(path: Path | None = None) -> list[EquitySnapshot]:
    p = path or equity_curve_path()
    if not p.exists():
        return []
    out: list[EquitySnapshot] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(EquitySnapshot.from_json(line))
    return out


def append_snapshot(snap: EquitySnapshot, path: Path | None = None) -> None:
    p = path or equity_curve_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(snap.to_json() + "\n")


def snapshot(broker: Broker, stage: str, note: str = "") -> EquitySnapshot:
    info = broker.account_info()
    open_profit = info.equity - info.balance
    return EquitySnapshot(
        ts=datetime.now(UTC),
        balance=info.balance,
        equity=info.equity,
        open_profit=round(open_profit, 2),
        stage=stage,
        note=note,
    )


def _start_of_day_utc(now: datetime, hour: int) -> datetime:
    """Return the most recent 'session reset' timestamp in UTC."""
    reset_today = datetime.combine(now.date(), time(hour=hour, tzinfo=UTC))
    return reset_today if now >= reset_today else reset_today - timedelta(days=1)


def _start_of_day_equity(
    curve: list[EquitySnapshot], now: datetime, reset_hour: int
) -> float | None:
    """Equity at the most recent start-of-day boundary."""
    reset = _start_of_day_utc(now, reset_hour)
    before = [s for s in curve if s.ts <= reset]
    if not before:
        return curve[0].equity if curve else None
    return before[-1].equity


def compute_rule_status(
    info: AccountInfo,
    curve: list[EquitySnapshot] | None = None,
    now: datetime | None = None,
) -> RuleStatus:
    """Does the current equity violate any FundedNext rule or internal hard stop?"""
    cfg = fundednext()
    reset_hour = int(cfg["drawdown"]["start_of_day_utc_hour"])
    curve = curve if curve is not None else read_equity_curve()
    now = now or datetime.now(UTC)

    initial = float(cfg["account"]["initial_balance"])
    sod_equity = _start_of_day_equity(curve, now, reset_hour) or initial

    daily_loss = max(0.0, sod_equity - info.equity)
    daily_dd_pct = 100.0 * daily_loss / sod_equity if sod_equity else 0.0

    total_loss = max(0.0, initial - info.equity)
    max_dd_pct = 100.0 * total_loss / initial if initial else 0.0

    daily_limit = float(cfg["drawdown"]["daily_loss_limit_pct"])
    daily_hard = float(cfg["drawdown"]["daily_loss_hard_stop_pct"])
    max_limit = float(cfg["drawdown"]["max_drawdown_pct"])
    max_hard = float(cfg["drawdown"]["max_drawdown_hard_stop_pct"])

    reasons: list[str] = []
    if daily_dd_pct >= daily_hard:
        reasons.append(f"daily DD {daily_dd_pct:.2f}% ≥ hard stop {daily_hard}%")
    if max_dd_pct >= max_hard:
        reasons.append(f"max DD {max_dd_pct:.2f}% ≥ hard stop {max_hard}%")
    if daily_dd_pct >= daily_limit:
        reasons.append(f"daily DD {daily_dd_pct:.2f}% ≥ FIRM LIMIT {daily_limit}%")
    if max_dd_pct >= max_limit:
        reasons.append(f"max DD {max_dd_pct:.2f}% ≥ FIRM LIMIT {max_limit}%")

    return RuleStatus(
        daily_dd_pct=round(daily_dd_pct, 3),
        max_dd_pct=round(max_dd_pct, 3),
        daily_dd_tripped=daily_dd_pct >= daily_limit,
        max_dd_tripped=max_dd_pct >= max_limit,
        hard_stop_daily_tripped=daily_dd_pct >= daily_hard,
        hard_stop_max_tripped=max_dd_pct >= max_hard,
        reasons=reasons,
    )


def is_in_violation(broker: Broker, now: datetime | None = None) -> RuleStatus:
    """Convenience: snapshot account info and check rules."""
    return compute_rule_status(broker.account_info(), now=now)

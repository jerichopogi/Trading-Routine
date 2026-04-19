"""Drawdown math on the equity curve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.account import (
    EquitySnapshot,
    append_snapshot,
    compute_rule_status,
    equity_curve_path,
    read_equity_curve,
)
from scripts.broker import AccountInfo


def _snap(ts: datetime, equity: float, balance: float | None = None) -> EquitySnapshot:
    return EquitySnapshot(
        ts=ts, balance=balance if balance is not None else equity,
        equity=equity, open_profit=0.0, stage="demo",
    )


def test_snapshot_roundtrip(isolated_memory: Path) -> None:
    assert isolated_memory.exists()
    now = datetime.now(UTC)
    snap = _snap(now, 50000.0)
    append_snapshot(snap)
    back = read_equity_curve()
    assert len(back) == 1
    assert back[0].equity == 50000.0


def test_no_curve_no_violation() -> None:
    info = AccountInfo(
        login=0, currency="USD",
        balance=50000.0, equity=50000.0, margin=0.0, free_margin=50000.0,
        server="demo",
    )
    rs = compute_rule_status(info, curve=[], now=datetime.now(UTC))
    assert not rs.any_firm_violation
    assert not rs.any_violation


def test_daily_dd_hard_stop_trips() -> None:
    """Equity dropped 4.5% intraday → hard stop (4%) tripped, firm limit (5%) not yet."""
    now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    sod = now.replace(hour=22, minute=0) - timedelta(days=1)  # 22:00 prior day
    curve = [_snap(sod, 50000.0), _snap(sod + timedelta(hours=1), 50000.0)]
    info = AccountInfo(
        login=0, currency="USD",
        balance=50000.0, equity=47750.0,  # down 4.5%
        margin=0.0, free_margin=47750.0, server="demo",
    )
    rs = compute_rule_status(info, curve=curve, now=now)
    assert rs.hard_stop_daily_tripped
    assert not rs.daily_dd_tripped          # firm limit not hit
    assert rs.daily_dd_pct > 4.0


def test_daily_dd_firm_limit_trips() -> None:
    now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    sod = now.replace(hour=22, minute=0) - timedelta(days=1)
    curve = [_snap(sod, 50000.0)]
    info = AccountInfo(
        login=0, currency="USD",
        balance=50000.0, equity=47000.0,  # down 6%
        margin=0.0, free_margin=47000.0, server="demo",
    )
    rs = compute_rule_status(info, curve=curve, now=now)
    assert rs.daily_dd_tripped
    assert rs.hard_stop_daily_tripped
    assert rs.daily_dd_pct >= 5.0


def test_max_dd_hard_stop_trips() -> None:
    now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    info = AccountInfo(
        login=0, currency="USD",
        balance=50000.0, equity=45500.0,  # down 9% from initial
        margin=0.0, free_margin=45500.0, server="demo",
    )
    rs = compute_rule_status(info, curve=[_snap(now, 45500.0)], now=now)
    assert rs.hard_stop_max_tripped
    assert rs.max_dd_pct > 8.0


def test_compliant_equity_no_violation() -> None:
    now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    info = AccountInfo(
        login=0, currency="USD",
        balance=50000.0, equity=50500.0,  # up 1%
        margin=0.0, free_margin=50500.0, server="demo",
    )
    sod = now.replace(hour=22, minute=0) - timedelta(days=1)
    rs = compute_rule_status(info, curve=[_snap(sod, 50000.0)], now=now)
    assert not rs.any_violation
    assert not rs.any_firm_violation


def test_equity_curve_path_respects_memory_dir(isolated_memory: Path) -> None:
    assert str(isolated_memory) in str(equity_curve_path())

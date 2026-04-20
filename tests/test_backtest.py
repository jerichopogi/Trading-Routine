"""Backtest Phase 1 — deterministic detection + simulation on synthetic bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.backtest import (
    BacktestConfig,
    BreakoutSignal,
    compute_stats,
    detect_london_breakout,
    run_backtest,
)
from scripts.broker.base import Bar, OrderSide


# ---------- helpers ----------

def _bar(t: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=h, low=low, close=c, tick_volume=100)


def _flat_bars(start: datetime, count: int, step: timedelta, price: float) -> list[Bar]:
    """Bars at a flat price — used to pad days with no action."""
    return [_bar(start + step * i, price, price, price, price) for i in range(count)]


# ---------- detect_london_breakout ----------

def test_detect_bull_break() -> None:
    # UTC day Wed 2026-04-22. Asian range: 00:00-07:00 at 1.1700-1.1720.
    # London bar at 07:00 closes above 1.1720 → bull break.
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    # Asian: 28 M15 bars (7 hours × 4). Range 1.1700 to 1.1720.
    for i in range(28):
        t = day + m15 * i
        bars.append(_bar(t, 1.17100, 1.17200, 1.17000, 1.17150))
    # 07:00 London bar: closes at 1.17260 — break above.
    bars.append(_bar(day + m15 * 28, 1.17150, 1.17280, 1.17150, 1.17260))

    sig = detect_london_breakout(bars, min_range_pips=20.0, pip_size=0.0001)
    assert sig is not None
    assert sig.side == OrderSide.BUY
    assert sig.entry == 1.17200  # retest of Asian high
    assert sig.sl == pytest.approx(1.17100, abs=1e-9)  # Asian mid
    # TP = break_close + 1.5 × range
    assert sig.tp == pytest.approx(1.17260 + 1.5 * 0.00200, abs=1e-9)


def test_detect_bear_break() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(28):
        t = day + m15 * i
        bars.append(_bar(t, 1.17100, 1.17200, 1.17000, 1.17150))
    # 07:00 bar closes at 1.16950 — break below.
    bars.append(_bar(day + m15 * 28, 1.17150, 1.17150, 1.16940, 1.16950))

    sig = detect_london_breakout(bars, min_range_pips=20.0, pip_size=0.0001)
    assert sig is not None
    assert sig.side == OrderSide.SELL
    assert sig.entry == 1.17000  # retest of Asian low


def test_detect_range_too_small_returns_none() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    # Range only 10 pips — filter rejects
    bars = [
        _bar(day + m15 * i, 1.17100, 1.17110, 1.17100, 1.17105)
        for i in range(28)
    ]
    bars.append(_bar(day + m15 * 28, 1.17105, 1.17200, 1.17105, 1.17180))
    assert detect_london_breakout(bars, min_range_pips=20.0, pip_size=0.0001) is None


def test_detect_no_break_returns_none() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(28):
        bars.append(_bar(day + m15 * i, 1.17100, 1.17200, 1.17000, 1.17150))
    # London bars stay inside range
    for i in range(28, 36):
        bars.append(_bar(day + m15 * i, 1.17150, 1.17180, 1.17120, 1.17160))
    assert detect_london_breakout(bars) is None


# ---------- run_backtest ----------

def _build_breakout_day(
    day_start: datetime, break_direction: str = "bull",
) -> list[Bar]:
    """Synthesize one breakout day's M15 bars: Asian range + break + retest + run to TP."""
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    # Asian 00:00-07:00 — 28 bars, range 1.1700 to 1.1720
    for i in range(28):
        bars.append(_bar(day_start + m15 * i, 1.17100, 1.17200, 1.17000, 1.17150))

    if break_direction == "bull":
        # 07:00 break: closes 1.17260
        bars.append(_bar(day_start + m15 * 28, 1.17150, 1.17280, 1.17150, 1.17260))
        # 07:15 pullback to entry (1.17200)
        bars.append(_bar(day_start + m15 * 29, 1.17260, 1.17265, 1.17195, 1.17220))
        # 07:30 onwards: runs up toward TP = 1.17560 (break_close + 1.5 × 0.00200)
        for i in range(30, 60):
            bars.append(_bar(day_start + m15 * i, 1.17220, 1.17600, 1.17200, 1.17550))
    else:  # bear
        bars.append(_bar(day_start + m15 * 28, 1.17150, 1.17150, 1.16940, 1.16950))
        bars.append(_bar(day_start + m15 * 29, 1.16950, 1.17010, 1.16940, 1.16980))
        for i in range(30, 60):
            bars.append(_bar(day_start + m15 * i, 1.16980, 1.17000, 1.16600, 1.16650))
    return bars


def test_simulator_bull_breakout_hits_tp() -> None:
    # Wed 2026-04-22 (UTC weekday)
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    bars = _build_breakout_day(day, "bull")
    trades, _ = run_backtest(bars, config=BacktestConfig())
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    t = filled[0]
    assert t.side == "buy"
    assert t.exit_reason == "tp"
    # R ≈ (tp - fill) / (fill - sl). fill ≈ entry + spread
    assert t.r_multiple is not None and t.r_multiple > 1.5


def test_simulator_bear_breakout_hits_tp() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    bars = _build_breakout_day(day, "bear")
    trades, _ = run_backtest(bars, config=BacktestConfig())
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    t = filled[0]
    assert t.side == "sell"
    assert t.exit_reason == "tp"


def test_simulator_pending_expires_when_no_fill() -> None:
    # Asian range forms, break happens, but price never retraces to entry.
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(28):
        bars.append(_bar(day + m15 * i, 1.17100, 1.17200, 1.17000, 1.17150))
    # Break and keep running up, no retest
    for i in range(28, 60):
        p = 1.17260 + 0.00010 * (i - 28)  # monotonic up
        bars.append(_bar(day + m15 * i, p - 0.00010, p + 0.00010, p - 0.00005, p))

    trades, _ = run_backtest(bars, config=BacktestConfig(pending_expire_hours=2))
    # No filled trades; one expired record
    assert not any(t.entry_fill is not None for t in trades)
    assert any(t.exit_reason == "expired" for t in trades)


def test_simulator_compute_stats_on_empty_is_safe() -> None:
    stats = compute_stats([], [], BacktestConfig())
    assert stats.total_signals == 0
    assert stats.filled == 0
    assert stats.win_rate_pct == 0.0


def test_simulator_sl_hit_records_negative_r() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(28):
        bars.append(_bar(day + m15 * i, 1.17100, 1.17200, 1.17000, 1.17150))
    # Bull break + retest + crash to SL (mid = 1.17100)
    bars.append(_bar(day + m15 * 28, 1.17150, 1.17280, 1.17150, 1.17260))
    bars.append(_bar(day + m15 * 29, 1.17260, 1.17265, 1.17195, 1.17220))  # fills at 1.17200
    bars.append(_bar(day + m15 * 30, 1.17220, 1.17220, 1.17050, 1.17080))  # hits SL @ 1.17100
    # Pad rest of day
    for i in range(31, 60):
        bars.append(_bar(day + m15 * i, 1.17080, 1.17085, 1.17075, 1.17080))

    trades, _ = run_backtest(bars, config=BacktestConfig())
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    t = filled[0]
    assert t.exit_reason == "sl"
    assert t.r_multiple is not None and t.r_multiple < 0

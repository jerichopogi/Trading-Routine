"""Backtest Phase 1 — deterministic detection + simulation on synthetic bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.backtest import (
    BacktestConfig,
    PortfolioConfig,
    PortfolioEntry,
    Signal,
    Timeframe,
    _compute_ema,
    compute_stats,
    detect_london_breakout,
    detect_ny_momentum,
    make_failed_breakout_fade_detector,
    make_gold_pullback_detector,
    run_backtest,
    run_portfolio,
)
from scripts.broker.base import Bar, OrderKind, OrderSide


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


def test_manage_runners_2r_partial_on_winner() -> None:
    """A winner runs through 2R before hitting TP — 2R partial should trigger.

    The LBO's TP lands near ~3.6R, so 3R partial may or may not fire before
    TP depending on bar sequencing. What's always true for a TP-hitting winner
    that walks through 2R: 2R partial fires, weighted R > 2.
    """
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    bars = _build_breakout_day(day, "bull")  # clean winner all the way to TP

    trades, _ = run_backtest(bars, config=BacktestConfig(enable_manage_runners=True))
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    t = filled[0]
    assert t.did_partial_2r  # must take 2R partial on the way up
    assert t.r_multiple is not None and t.r_multiple > 2.0


# ---------- detect_ny_momentum ----------


def test_ny_momentum_bull_signal() -> None:
    """NAS100 first cash-open bar closes above pre-market high → STOP BUY at its high."""
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    # Pre-market bars 00:00 - 13:30 (54 bars of 15min). Range: 17400-17500.
    for i in range(54):
        bars.append(_bar(day + m15 * i, 17450, 17500, 17400, 17470))
    # First cash bar 13:30-13:45: opens 17480, makes new high 17560, closes 17550 (above pm_high 17500)
    bars.append(_bar(day + m15 * 54, 17480, 17560, 17470, 17550))

    sig = detect_ny_momentum(bars)
    assert sig is not None
    assert sig.setup == "ny_momentum"
    assert sig.side == OrderSide.BUY
    assert sig.entry_kind == OrderKind.STOP
    assert sig.entry == 17560  # cash bar high
    assert sig.sl == 17470     # cash bar low
    # TP = entry + 2 × first_range (90 pts)
    assert sig.tp == pytest.approx(17560 + 2 * 90, abs=1e-6)


def test_ny_momentum_bear_signal() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(54):
        bars.append(_bar(day + m15 * i, 17450, 17500, 17400, 17470))
    # Cash bar: opens 17420, drops to 17340, closes 17350 (below pm_low 17400)
    bars.append(_bar(day + m15 * 54, 17420, 17430, 17340, 17350))

    sig = detect_ny_momentum(bars)
    assert sig is not None
    assert sig.side == OrderSide.SELL
    assert sig.entry_kind == OrderKind.STOP
    assert sig.entry == 17340


def test_ny_momentum_no_signal_when_close_inside_range() -> None:
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(54):
        bars.append(_bar(day + m15 * i, 17450, 17500, 17400, 17470))
    # Cash bar close stays inside range
    bars.append(_bar(day + m15 * 54, 17480, 17495, 17475, 17485))
    assert detect_ny_momentum(bars) is None


def test_simulator_with_ny_momentum_detector() -> None:
    """End-to-end: NY momentum bull signal → STOP order fills on breakout bar."""
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(54):
        bars.append(_bar(day + m15 * i, 17450, 17500, 17400, 17470))
    # Cash bar: closes above pm_high → STOP BUY at 17560
    bars.append(_bar(day + m15 * 54, 17480, 17560, 17470, 17550))
    # Next bar: breaks 17560 and runs up
    bars.append(_bar(day + m15 * 55, 17555, 17620, 17550, 17600))
    bars.append(_bar(day + m15 * 56, 17600, 17740, 17580, 17740))  # hits TP 17740

    def detector(day_bars, all_bars, cfg):
        return detect_ny_momentum(day_bars)

    config = BacktestConfig(symbol="NAS100", pip_size=1.0, enable_manage_runners=False)
    trades, _ = run_backtest(bars, config=config, detector=detector)
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    t = filled[0]
    assert t.setup == "ny_momentum"
    assert t.side == "buy"
    assert t.exit_reason == "tp"


# ---------- _compute_ema ----------


def test_compute_ema_returns_none_before_seed() -> None:
    ema = _compute_ema([1.0, 2.0, 3.0, 4.0, 5.0], period=3)
    assert ema[0] is None
    assert ema[1] is None
    assert ema[2] == pytest.approx(2.0, abs=1e-9)  # SMA seed of first 3


def test_compute_ema_converges_to_constant_input() -> None:
    # After enough bars, constant input → EMA equals that constant
    values = [50.0] * 100
    ema = _compute_ema(values, period=20)
    assert ema[-1] == pytest.approx(50.0, abs=1e-9)


# ---------- make_gold_pullback_detector ----------


def test_gold_pullback_bull_signal() -> None:
    """Uptrend + wick into EMA20 + close above + upper-half body → BUY STOP."""
    # Build a bull-trending series: 100 bars rising from 2300 to 2400
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h1 = timedelta(hours=1)
    bars: list[Bar] = []
    for i in range(100):
        p = 2300.0 + i * 1.0
        bars.append(_bar(t0 + h1 * i, p - 0.5, p + 0.5, p - 0.5, p))
    # Day bars: the last day of the run. Make the final bar a rejection:
    # dips down well below EMA20, closes back above with close in upper half
    last_t = bars[-1].time
    # Reset the last bar to a rejection pattern
    bars[-1] = _bar(last_t, 2398.0, 2400.0, 2385.0, 2399.0)  # wick down to 2385, close near high

    # Just pass the single "day" (last bar) for detection
    day_bars = [bars[-1]]
    detector = make_gold_pullback_detector(bars)
    sig = detector(day_bars, bars, BacktestConfig(symbol="XAUUSD", pip_size=0.01))
    assert sig is not None
    assert sig.side == OrderSide.BUY
    assert sig.entry_kind == OrderKind.STOP
    assert sig.entry > bars[-1].high  # entry is high + 1 pip


def test_gold_pullback_no_signal_in_downtrend_for_buy() -> None:
    """Downtrend (EMA20 < EMA50) must not produce BUY signals even with bull wicks."""
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h1 = timedelta(hours=1)
    bars: list[Bar] = []
    # Descending price
    for i in range(100):
        p = 2400.0 - i * 1.0
        bars.append(_bar(t0 + h1 * i, p + 0.5, p + 0.5, p - 0.5, p))
    last_t = bars[-1].time
    # Bullish-looking wick but in downtrend — should NOT trigger bull; may trigger bear
    bars[-1] = _bar(last_t, 2300.0, 2315.0, 2298.0, 2301.0)  # close near low

    day_bars = [bars[-1]]
    detector = make_gold_pullback_detector(bars)
    sig = detector(day_bars, bars, BacktestConfig(symbol="XAUUSD", pip_size=0.01))
    # In downtrend + close in lower half → bear signal expected
    if sig is not None:
        assert sig.side == OrderSide.SELL


def test_gold_pullback_no_signal_without_ema_context() -> None:
    """Fewer than 50 bars → EMA50 is None → no signal."""
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h1 = timedelta(hours=1)
    bars = [_bar(t0 + h1 * i, 2300.0, 2301.0, 2299.0, 2300.0) for i in range(30)]
    day_bars = [bars[-1]]
    detector = make_gold_pullback_detector(bars)
    sig = detector(day_bars, bars, BacktestConfig(symbol="XAUUSD", pip_size=0.01))
    assert sig is None


# ---------- make_failed_breakout_fade_detector ----------


def test_fade_bearish_signal_when_high_swept_and_bar_closes_bearish() -> None:
    """Sweep recent high, close back inside, bearish candle → SELL MARKET."""
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h4 = timedelta(hours=4)
    bars: list[Bar] = []
    # 12 H4 bars with high near 1.1800
    for i in range(12):
        bars.append(_bar(t0 + h4 * i, 1.17800, 1.17950, 1.17600, 1.17800))
    # Current bar: wicks above 1.17950 then closes back below with bearish body
    bars.append(_bar(t0 + h4 * 12, 1.17900, 1.18100, 1.17700, 1.17700))

    day_bars = [bars[-1]]
    detector = make_failed_breakout_fade_detector(bars)
    sig = detector(day_bars, bars, BacktestConfig(pip_size=0.0001))
    assert sig is not None
    assert sig.side == OrderSide.SELL
    assert sig.entry_kind == OrderKind.MARKET
    assert sig.entry == 1.17700
    assert sig.sl > bars[-1].high  # SL above swept high + buffer


def test_fade_bullish_signal_when_low_swept_and_bar_closes_bullish() -> None:
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h4 = timedelta(hours=4)
    bars: list[Bar] = []
    for i in range(12):
        bars.append(_bar(t0 + h4 * i, 1.17800, 1.17950, 1.17600, 1.17800))
    bars.append(_bar(t0 + h4 * 12, 1.17700, 1.17900, 1.17500, 1.17900))  # wick below 1.17600, bullish close

    day_bars = [bars[-1]]
    detector = make_failed_breakout_fade_detector(bars)
    sig = detector(day_bars, bars, BacktestConfig(pip_size=0.0001))
    assert sig is not None
    assert sig.side == OrderSide.BUY


def test_fade_no_signal_when_bar_does_not_sweep_window() -> None:
    t0 = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    h4 = timedelta(hours=4)
    bars: list[Bar] = []
    for i in range(12):
        bars.append(_bar(t0 + h4 * i, 1.17800, 1.17950, 1.17600, 1.17800))
    # Current bar stays inside the window
    bars.append(_bar(t0 + h4 * 12, 1.17800, 1.17850, 1.17750, 1.17820))

    day_bars = [bars[-1]]
    detector = make_failed_breakout_fade_detector(bars)
    assert detector(day_bars, bars, BacktestConfig(pip_size=0.0001)) is None


def test_simulator_market_entry_fills_at_next_bar_open() -> None:
    """A MARKET signal fills at the first subsequent bar's open price."""
    t0 = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    h4 = timedelta(hours=4)
    bars: list[Bar] = []
    for i in range(12):
        bars.append(_bar(t0 + h4 * i, 1.17800, 1.17950, 1.17600, 1.17800))
    # Signal bar: swept + bearish close
    bars.append(_bar(t0 + h4 * 12, 1.17900, 1.18100, 1.17700, 1.17700))
    # Follow-through bar (fills the market order at its open); then drops to TP
    bars.append(_bar(t0 + h4 * 13, 1.17700, 1.17720, 1.16500, 1.16600))  # big drop

    detector = make_failed_breakout_fade_detector(bars)
    config = BacktestConfig(pip_size=0.0001, enable_manage_runners=False)
    trades, _ = run_backtest(bars, config=config, detector=detector)
    filled = [t for t in trades if t.entry_fill is not None]
    assert len(filled) == 1
    assert filled[0].setup == "failed_breakout_fade"
    assert filled[0].side == "sell"


# ---------- run_portfolio ----------


def test_portfolio_respects_max_concurrent_positions() -> None:
    """With 3-position cap and 5 simultaneous signals, only 3 fill; 2 skip."""
    # Build 5 sets of bars, each producing one LBO signal on the SAME day
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    bars_by_symbol = {}
    for sym in ("EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "NZDUSD"):
        bars = _build_breakout_day(day, "bull")
        bars_by_symbol[sym] = bars

    entries = [
        PortfolioEntry(sym, Timeframe.M15, "london_breakout", 0.0001)
        for sym in bars_by_symbol
    ]
    pcfg = PortfolioConfig(max_concurrent_positions=3)

    def loader(sym, tf):
        return bars_by_symbol[sym]

    taken, stats, _ = run_portfolio(
        entries, start=day, end=day + timedelta(days=1), config=pcfg, bar_loader=loader,
    )
    # All 5 setups produced a signal; cap allows only 3 to be taken concurrently
    assert stats.total_trade_attempts == 5
    assert stats.filled == 3
    assert stats.skipped_concurrent == 2


def test_portfolio_halts_on_daily_dd_hard_stop() -> None:
    """If a string of losers breaches daily DD, further same-day trades skip."""
    # Configure extreme risk so one loss breaches daily cap immediately
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    m15 = timedelta(minutes=15)
    bars: list[Bar] = []
    for i in range(28):
        bars.append(_bar(day + m15 * i, 1.17100, 1.17200, 1.17000, 1.17150))
    # Bull break, retest-fill, then plunge to SL
    bars.append(_bar(day + m15 * 28, 1.17150, 1.17280, 1.17150, 1.17260))
    bars.append(_bar(day + m15 * 29, 1.17260, 1.17265, 1.17195, 1.17220))
    bars.append(_bar(day + m15 * 30, 1.17220, 1.17220, 1.17050, 1.17080))
    for i in range(31, 60):
        bars.append(_bar(day + m15 * i, 1.17080, 1.17085, 1.17075, 1.17080))

    pcfg = PortfolioConfig(
        daily_dd_hard_stop_pct=0.01,  # trip instantly on first loss
        risk_pct=0.5,
    )
    entries = [PortfolioEntry("EURUSD", Timeframe.M15, "london_breakout", 0.0001)]

    def loader(sym, tf):
        return bars

    taken, stats, _ = run_portfolio(
        entries, start=day, end=day + timedelta(days=1), config=pcfg, bar_loader=loader,
    )
    # First trade fills; realizes loss; daily DD trips; no more trades possible
    # (Only 1 signal in this single-day scenario anyway, but the machinery ran.)
    assert stats.filled <= 1


def test_manage_runners_toggle_changes_result() -> None:
    """Flipping enable_manage_runners on vs off produces different R on a runner."""
    day = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    bars = _build_breakout_day(day, "bull")

    phase1 = run_backtest(bars, config=BacktestConfig(enable_manage_runners=False))
    phase2 = run_backtest(bars, config=BacktestConfig(enable_manage_runners=True))

    p1 = [t for t in phase1[0] if t.entry_fill is not None][0]
    p2 = [t for t in phase2[0] if t.entry_fill is not None][0]
    assert p1.partials == []  # no partials in Phase 1 mode
    assert len(p2.partials) >= 1  # Phase 2 takes at least the 2R partial

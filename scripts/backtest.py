"""Backtest -- Phase 2a: London Breakout + manage-runners on EURUSD.

Pulls historical M15 bars from MT5, walks them chronologically, detects the
London Breakout setup per `memory/playbook.md`, simulates pending-order fills
against bar extremes, and applies the manage-runners rule table (breakeven
at 1R, 50% partial at 2R with SL to +1R, 20% partial at 3R with SL to +2R,
1R-behind trailing above 3R). Reports aggregate stats + per-trade CSV.

Deliberate Phase 2a scope (to flag in the report):
  - Single setup (London Breakout), single symbol (EURUSD).
  - manage-runners integrated (toggleable via --no-manage-runners for
    baseline comparison vs Phase 1).
  - No news filter, no LLM rubric -- items 2/3/5 forced true.
  - Single position at a time. Real guardrails allow up to 3 concurrent.
  - Fixed 0.5% risk (B-grade).
  - Daily DD hard-stop enforced at 4% to mirror `config/fundednext.yml`.

Usage:
    python -m scripts.backtest --from 2025-04-01 --to 2026-04-01
    python -m scripts.backtest --from 2025-04-01 --to 2026-04-01 --no-manage-runners
    python -m scripts.backtest --from 2025-04-01 --to 2026-04-01 --output results/
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time as dt_time, timedelta
from pathlib import Path

from .broker.base import Bar, OrderKind, OrderSide, Timeframe


# ----------------------------- Data loading ---------------------------------

def load_mt5_history(symbol: str, tf: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    """Pull bars from MT5 terminal via `copy_rates_range`.

    Requires MT5 terminal running + the `MetaTrader5` pip package. Loads
    env from .env so MT5_LOGIN / MT5_PASSWORD / MT5_SERVER are available.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    import MetaTrader5 as mt5  # type: ignore[import-not-found]

    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    try:
        tf_map = {
            Timeframe.M15: mt5.TIMEFRAME_M15,
            Timeframe.H1: mt5.TIMEFRAME_H1,
            Timeframe.H4: mt5.TIMEFRAME_H4,
            Timeframe.D1: mt5.TIMEFRAME_D1,
        }
        if tf not in tf_map:
            raise ValueError(f"Timeframe {tf} not supported by backtest loader")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Cannot select symbol {symbol!r}")
        raw = mt5.copy_rates_range(symbol, tf_map[tf], start, end)
        if raw is None or len(raw) == 0:
            raise RuntimeError(
                f"MT5 returned no bars for {symbol} {tf.value} "
                f"{start.date()}..{end.date()} -- broker may not retain this far back."
            )
        return [
            Bar(
                time=datetime.fromtimestamp(int(r["time"]), tz=UTC),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
            )
            for r in raw
        ]
    finally:
        mt5.shutdown()


# --------------------------- Setup detection --------------------------------

@dataclass(frozen=True)
class Signal:
    """Generic setup signal — both playbook setups produce this shape.

    `entry_kind` controls how the pending order triggers:
      - LIMIT: fills when price pulls BACK to entry (buy → ask <= entry)
      - STOP:  fills when price BREAKS through entry (buy → ask >= entry)
    """
    setup: str                     # "london_breakout" | "ny_momentum"
    signal_time: datetime
    side: OrderSide
    entry_kind: OrderKind
    entry: float
    sl: float
    tp: float


# Kept as alias so any external callers importing BreakoutSignal don't break.
BreakoutSignal = Signal


def detect_london_breakout(
    day_bars: list[Bar], *, min_range_pips: float = 20.0, pip_size: float = 0.0001,
) -> Signal | None:
    """Detect London Breakout per playbook #1: Asian range -> retest entry (LIMIT)."""
    asian = [b for b in day_bars if b.time.time() < dt_time(7, 0)]
    london = [
        b for b in day_bars
        if dt_time(7, 0) <= b.time.time() < dt_time(9, 0)
    ]
    if not asian or not london:
        return None

    asian_high = max(b.high for b in asian)
    asian_low = min(b.low for b in asian)
    asian_range = asian_high - asian_low
    if asian_range / pip_size < min_range_pips:
        return None
    asian_mid = (asian_high + asian_low) / 2.0

    for bar in london:
        if bar.close > asian_high:
            return Signal(
                setup="london_breakout", signal_time=bar.time, side=OrderSide.BUY,
                entry_kind=OrderKind.LIMIT,
                entry=asian_high, sl=asian_mid, tp=bar.close + 1.5 * asian_range,
            )
        if bar.close < asian_low:
            return Signal(
                setup="london_breakout", signal_time=bar.time, side=OrderSide.SELL,
                entry_kind=OrderKind.LIMIT,
                entry=asian_low, sl=asian_mid, tp=bar.close - 1.5 * asian_range,
            )
    return None


def _compute_ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average. Returns None for the first period-1 entries."""
    if period <= 0 or not values:
        return [None] * len(values)
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    # Seed with SMA of first `period` values
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def make_gold_pullback_detector(all_bars: list[Bar]):
    """Build a detector closure for XAUUSD Gold Trend Pullback.

    Pre-computes EMA20 and EMA50 across the full bar series (typically H1).
    The returned detector is called per day; it looks up EMA values by bar
    timestamp and scans for the rejection-wick pattern.

    Rejection pattern (bull; mirror for bear):
      - EMA20 > EMA50 at the rejection bar (uptrend)
      - Bar's low <= EMA20 (touched or wicked below EMA20)
      - Bar's close > EMA20 (reclaimed)
      - Close in upper half of bar range (lower wick dominant)
    Entry: bar.high + 1 point (STOP, break of rejection high)
    SL:    EMA50 at signal bar (widest structural stop)
    TP:    entry + 2R
    """
    closes = [b.close for b in all_bars]
    ema20 = _compute_ema(closes, 20)
    ema50 = _compute_ema(closes, 50)
    idx_by_time = {b.time: i for i, b in enumerate(all_bars)}

    def detector(day_bars: list[Bar], _all_bars: list[Bar], config: "BacktestConfig") -> Signal | None:
        for bar in day_bars:
            i = idx_by_time.get(bar.time)
            if i is None or i == 0:
                continue
            e20 = ema20[i]
            e50 = ema50[i]
            if e20 is None or e50 is None:
                continue
            rng = bar.high - bar.low
            if rng <= 0:
                continue
            close_pos = (bar.close - bar.low) / rng  # 0..1, upper=bullish
            # Bull pullback-rejection
            if e20 > e50 and bar.low <= e20 <= bar.close and close_pos > 0.6:
                entry = bar.high + 1.0 * config.pip_size
                sl = e50
                if entry <= sl:
                    continue
                tp = entry + 2.0 * (entry - sl)
                return Signal(
                    setup="gold_pullback", signal_time=bar.time, side=OrderSide.BUY,
                    entry_kind=OrderKind.STOP, entry=entry, sl=sl, tp=tp,
                )
            # Bear pullback-rejection
            if e20 < e50 and bar.high >= e20 >= bar.close and close_pos < 0.4:
                entry = bar.low - 1.0 * config.pip_size
                sl = e50
                if entry >= sl:
                    continue
                tp = entry - 2.0 * (sl - entry)
                return Signal(
                    setup="gold_pullback", signal_time=bar.time, side=OrderSide.SELL,
                    entry_kind=OrderKind.STOP, entry=entry, sl=sl, tp=tp,
                )
        return None

    return detector


def make_failed_breakout_fade_detector(
    all_bars: list[Bar], *, lookback_bars: int = 12, stop_buffer_pips: float = 5.0,
):
    """Build a detector closure for FX Failed-Breakout Fade (playbook #4).

    Approximation of the playbook rule:
      - Over the last `lookback_bars` (H4 bars = 48 hours), find the highest
        high (recent resistance). If the CURRENT bar's HIGH exceeded that
        level but its CLOSE is back BELOW it AND the bar is bearish
        (close < open) -> bearish fade. Mirror for bullish fade.
      - Entry: MARKET at bar close
      - SL: the break-extreme + `stop_buffer_pips` * pip_size
      - TP: entry +/- 2R

    Simplifications vs playbook:
      - Uses N-bar lookback high/low, not formal swing pivots
      - "Within 2 hours" simplified to "within the same bar" (break-and-reclaim
        intrabar). Works for H4 bars, less meaningful on lower timeframes.
      - No "first hour of London/NY" filter
    """
    idx_by_time = {b.time: i for i, b in enumerate(all_bars)}

    def detector(day_bars: list[Bar], _all_bars: list[Bar], config: "BacktestConfig") -> Signal | None:
        buf = stop_buffer_pips * config.pip_size
        for bar in day_bars:
            i = idx_by_time.get(bar.time)
            if i is None or i < lookback_bars:
                continue
            # Lookback window ends at the bar BEFORE current
            window = all_bars[i - lookback_bars:i]
            window_high = max(b.high for b in window)
            window_low = min(b.low for b in window)
            bearish = bar.close < bar.open
            bullish = bar.close > bar.open

            # Bearish fade: swept recent high, closed back below, bearish bar
            if bar.high > window_high and bar.close < window_high and bearish:
                sl = bar.high + buf
                risk = sl - bar.close
                if risk <= 0:
                    continue
                tp = bar.close - 2.0 * risk
                return Signal(
                    setup="failed_breakout_fade", signal_time=bar.time,
                    side=OrderSide.SELL, entry_kind=OrderKind.MARKET,
                    entry=bar.close, sl=sl, tp=tp,
                )
            # Bullish fade: swept recent low, closed back above, bullish bar
            if bar.low < window_low and bar.close > window_low and bullish:
                sl = bar.low - buf
                risk = bar.close - sl
                if risk <= 0:
                    continue
                tp = bar.close + 2.0 * risk
                return Signal(
                    setup="failed_breakout_fade", signal_time=bar.time,
                    side=OrderSide.BUY, entry_kind=OrderKind.MARKET,
                    entry=bar.close, sl=sl, tp=tp,
                )
        return None

    return detector


def detect_ny_momentum(
    day_bars: list[Bar], *, cash_open_hour: int = 13, cash_open_minute: int = 30,
) -> Signal | None:
    """Detect NY Open Momentum per playbook #2.

    Pre-market range: high/low of bars 00:00 UTC up to cash open (13:30 UTC).
    First cash bar: 13:30-13:45 UTC (M15). If its CLOSE is outside the
    pre-market range, we wait for the NEXT bar to break its high (long)
    or low (short) — a STOP entry.

    SL: opposite side of first-15m bar (+/- 1 pip for noise).
    TP: entry + 2 × first-15m-range (for long; mirror for short).

    Returns a STOP-kind Signal or None.
    """
    cash_open = dt_time(cash_open_hour, cash_open_minute)
    pre_market = [b for b in day_bars if b.time.time() < cash_open]
    cash_bar = next(
        (b for b in day_bars if b.time.time() == cash_open), None
    )
    if not pre_market or cash_bar is None:
        return None

    pm_high = max(b.high for b in pre_market)
    pm_low = min(b.low for b in pre_market)
    first_range = cash_bar.high - cash_bar.low
    if first_range <= 0:
        return None

    if cash_bar.close > pm_high:
        # Bull signal: entry = first-bar high (STOP), SL = first-bar low, TP = entry + 2 × range
        return Signal(
            setup="ny_momentum", signal_time=cash_bar.time, side=OrderSide.BUY,
            entry_kind=OrderKind.STOP,
            entry=cash_bar.high,
            sl=cash_bar.low,
            tp=cash_bar.high + 2.0 * first_range,
        )
    if cash_bar.close < pm_low:
        return Signal(
            setup="ny_momentum", signal_time=cash_bar.time, side=OrderSide.SELL,
            entry_kind=OrderKind.STOP,
            entry=cash_bar.low,
            sl=cash_bar.high,
            tp=cash_bar.low - 2.0 * first_range,
        )
    return None


# -------------------------- Simulated trade record --------------------------

@dataclass
class SimulatedTrade:
    symbol: str
    side: str                      # "buy" | "sell"
    setup: str
    signal_time: datetime
    fill_time: datetime | None
    close_time: datetime | None
    entry_planned: float
    entry_fill: float | None
    sl: float                      # ORIGINAL stop loss (never mutated)
    tp: float
    exit_price: float | None
    exit_reason: str | None        # "tp" | "sl" | "eow" | "dd_stop" | "end" | "expired"
    r_multiple: float | None       # WEIGHTED R across partials + final exit
    pnl_r: float | None            # realized R for stats (None if not filled)
    # manage-runners state (Phase 2a)
    current_sl: float = 0.0        # mutable SL (may move up via BE / partial steps / trail)
    partials: list[tuple[float, float]] = field(default_factory=list)  # (fraction_of_original, r_realized)
    did_breakeven: bool = False
    did_partial_2r: bool = False
    did_partial_3r: bool = False
    peak_fav_r: float = 0.0        # max favorable R touched so far (for trailing)


# -------------------------------- Simulator ---------------------------------

@dataclass(frozen=True)
class BacktestConfig:
    symbol: str = "EURUSD"
    initial_balance: float = 50_000.0
    risk_pct: float = 0.5          # B-grade default; A-grade sizing not modeled
    pip_size: float = 0.0001
    spread_pips: float = 0.2
    min_asian_range_pips: float = 20.0
    pending_expire_hours: int = 8  # cancel if not filled by end of London session
    daily_dd_hard_stop_pct: float = 4.0
    max_dd_hard_stop_pct: float = 8.0
    eow_flatten_utc_hour: int = 21  # Friday 21:00 UTC
    # Phase 2a — manage-runners rule table (matches scripts/management.py)
    enable_manage_runners: bool = True
    breakeven_min_r: float = 1.0
    partial_1_trigger_r: float = 2.0
    partial_1_fraction: float = 0.50
    partial_1_sl_to_r: float = 1.0
    partial_2_trigger_r: float = 3.0
    partial_2_fraction: float = 0.20
    partial_2_sl_to_r: float = 2.0
    trail_activate_r: float = 3.0
    trail_r_step: float = 1.0


def _default_detector(day_bars: list[Bar], all_bars: list[Bar], config: "BacktestConfig") -> Signal | None:
    return detect_london_breakout(
        day_bars, min_range_pips=config.min_asian_range_pips, pip_size=config.pip_size,
    )


def run_backtest(
    bars: list[Bar], *, config: BacktestConfig = BacktestConfig(),
    detector=None,
) -> tuple[list[SimulatedTrade], list[tuple[datetime, float]]]:
    """Walk bars chronologically; return (trades, equity_curve_points).

    `detector` is a callable `(day_bars, all_bars, config) -> Signal | None`.
    Default: London Breakout. Use `detect_ny_momentum` or
    `make_gold_pullback_detector(bars)` for the other setups.
    """
    if detector is None:
        detector = _default_detector
    if not bars:
        return [], []

    trades: list[SimulatedTrade] = []
    equity_curve: list[tuple[datetime, float]] = []

    balance = config.initial_balance
    peak_balance = balance
    spread = config.spread_pips * config.pip_size

    open_trade: SimulatedTrade | None = None
    pending: BreakoutSignal | None = None
    pending_placed_at: datetime | None = None

    current_day = None
    day_start_balance = balance
    day_already_traded = False  # one setup fill per day

    # Pre-index bars by UTC date for setup detection
    by_day: dict[object, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.time.date(), []).append(b)

    max_dd_hit = False

    for bar in bars:
        # --- day rollover -------------------------------------------------
        if current_day != bar.time.date():
            current_day = bar.time.date()
            day_start_balance = balance
            pending = None
            pending_placed_at = None
            day_already_traded = False

        # --- setup detection (once we've seen 09:00 bars for the day) ----
        if (
            open_trade is None
            and pending is None
            and not day_already_traded
        ):
            sig = detector(by_day[current_day], bars, config)
            if sig is not None and bar.time >= sig.signal_time:
                pending = sig
                pending_placed_at = bar.time

        # --- open position: SL (prior-bar) -> management -> TP / EOW ----
        if open_trade is not None:
            side_sign = 1.0 if open_trade.side == "buy" else -1.0
            risk_per_unit = abs(open_trade.entry_fill - open_trade.sl)

            exit_reason: str | None = None
            exit_price: float | None = None

            # Step 1 — adverse-extreme check against SL as it stood at START
            # of this bar. Conservative worst-case for loss accounting.
            hit_sl = (
                bar.low <= open_trade.current_sl
                if open_trade.side == "buy"
                else bar.high >= open_trade.current_sl
            )
            if hit_sl:
                exit_reason = "sl"
                exit_price = open_trade.current_sl

            # Step 2 — if still open, run management using favorable extreme.
            # Partials get recorded BEFORE the TP check so big-runner bars
            # that blow through 2R / 3R / TP in one move still credit partials.
            if exit_reason is None and config.enable_manage_runners:
                fav_extreme = bar.high if open_trade.side == "buy" else bar.low
                fav_r = (fav_extreme - open_trade.entry_fill) * side_sign / max(risk_per_unit, 1e-12)
                open_trade.peak_fav_r = max(open_trade.peak_fav_r, fav_r)

                def _new_sl_at_r(r_target: float) -> float:
                    return open_trade.entry_fill + r_target * risk_per_unit * side_sign

                def _tighten(new_sl: float) -> None:
                    if open_trade.side == "buy":
                        if new_sl > open_trade.current_sl:
                            open_trade.current_sl = new_sl
                    else:
                        if new_sl < open_trade.current_sl:
                            open_trade.current_sl = new_sl

                if fav_r >= config.breakeven_min_r and not open_trade.did_breakeven:
                    _tighten(open_trade.entry_fill)
                    open_trade.did_breakeven = True
                if fav_r >= config.partial_1_trigger_r and not open_trade.did_partial_2r:
                    open_trade.partials.append((config.partial_1_fraction, config.partial_1_trigger_r))
                    open_trade.did_partial_2r = True
                    _tighten(_new_sl_at_r(config.partial_1_sl_to_r))
                if fav_r >= config.partial_2_trigger_r and not open_trade.did_partial_3r:
                    open_trade.partials.append((config.partial_2_fraction, config.partial_2_trigger_r))
                    open_trade.did_partial_3r = True
                    _tighten(_new_sl_at_r(config.partial_2_sl_to_r))
                if fav_r > config.trail_activate_r and open_trade.did_partial_3r:
                    trail_r = fav_r - config.trail_r_step
                    _tighten(_new_sl_at_r(trail_r))

            # Step 3 — if still open, check TP for the remaining fraction.
            if exit_reason is None:
                hit_tp = (
                    bar.high >= open_trade.tp
                    if open_trade.side == "buy"
                    else bar.low <= open_trade.tp
                )
                if hit_tp:
                    exit_reason = "tp"
                    exit_price = open_trade.tp

            # Step 4 — EOW flatten
            if exit_reason is None and (
                bar.time.weekday() == 4
                and bar.time.time() >= dt_time(config.eow_flatten_utc_hour, 0)
            ):
                exit_reason = "eow"
                exit_price = bar.close

            if exit_reason:
                final_r = (exit_price - open_trade.entry_fill) * side_sign / max(risk_per_unit, 1e-12)
                remaining_frac = 1.0 - sum(f for f, _ in open_trade.partials)
                weighted_r = sum(f * r for f, r in open_trade.partials) + remaining_frac * final_r
                risk_dollars = day_start_balance * (config.risk_pct / 100.0)
                balance += weighted_r * risk_dollars
                peak_balance = max(peak_balance, balance)

                open_trade.close_time = bar.time
                open_trade.exit_price = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.r_multiple = weighted_r
                open_trade.pnl_r = weighted_r
                trades.append(open_trade)
                open_trade = None

        # --- pending trigger / expiry -------------------------------------
        if pending is not None and open_trade is None:
            # LIMIT: fills when price moves BACK to entry
            # STOP:  fills when price BREAKS through entry going further
            if pending.entry_kind == OrderKind.LIMIT:
                triggered = (
                    pending.side == OrderSide.BUY and bar.low <= pending.entry
                ) or (
                    pending.side == OrderSide.SELL and bar.high >= pending.entry
                )
                market_fill = False
            elif pending.entry_kind == OrderKind.STOP:
                triggered = (
                    pending.side == OrderSide.BUY and bar.high >= pending.entry
                ) or (
                    pending.side == OrderSide.SELL and bar.low <= pending.entry
                )
                market_fill = False
            else:  # MARKET — fills immediately at the first bar we see at/after signal_time
                triggered = bar.time >= pending.signal_time
                market_fill = True
            if triggered:
                if market_fill:
                    # For MARKET entries, fill at this bar's open (± spread)
                    fill = bar.open + spread if pending.side == OrderSide.BUY else bar.open - spread
                else:
                    fill = (
                        pending.entry + spread
                        if pending.side == OrderSide.BUY
                        else pending.entry - spread
                    )
                open_trade = SimulatedTrade(
                    symbol=config.symbol,
                    side=pending.side.value,
                    setup=pending.setup,
                    signal_time=pending.signal_time,
                    fill_time=bar.time,
                    close_time=None,
                    entry_planned=pending.entry,
                    entry_fill=fill,
                    sl=pending.sl,
                    tp=pending.tp,
                    exit_price=None,
                    exit_reason=None,
                    r_multiple=None,
                    pnl_r=None,
                    current_sl=pending.sl,
                )
                pending = None
                pending_placed_at = None
                day_already_traded = True
            elif (
                pending_placed_at is not None
                and (bar.time - pending_placed_at) > timedelta(hours=config.pending_expire_hours)
            ):
                trades.append(SimulatedTrade(
                    symbol=config.symbol, side=pending.side.value,
                    setup=pending.setup, signal_time=pending.signal_time,
                    fill_time=None, close_time=bar.time,
                    entry_planned=pending.entry, entry_fill=None,
                    sl=pending.sl, tp=pending.tp,
                    exit_price=None, exit_reason="expired",
                    r_multiple=None, pnl_r=None,
                ))
                pending = None
                pending_placed_at = None
                day_already_traded = True

        # --- daily + max DD gate -----------------------------------------
        equity = balance
        if open_trade is not None:
            side_sign = 1.0 if open_trade.side == "buy" else -1.0
            risk_per_unit = abs(open_trade.entry_fill - open_trade.sl)
            unreal_r = (bar.close - open_trade.entry_fill) * side_sign / max(risk_per_unit, 1e-12)
            risk_dollars = day_start_balance * (config.risk_pct / 100.0)
            equity = balance + unreal_r * risk_dollars

        daily_dd_pct = max(0.0, 100.0 * (day_start_balance - equity) / max(day_start_balance, 1e-9))
        max_dd_pct = max(0.0, 100.0 * (peak_balance - equity) / max(peak_balance, 1e-9))

        if max_dd_pct >= config.max_dd_hard_stop_pct:
            max_dd_hit = True

        if (
            open_trade is not None
            and (daily_dd_pct >= config.daily_dd_hard_stop_pct or max_dd_hit)
        ):
            # Force-close at bar close (weighted with any prior partials)
            side_sign = 1.0 if open_trade.side == "buy" else -1.0
            risk_per_unit = abs(open_trade.entry_fill - open_trade.sl)
            final_r = (bar.close - open_trade.entry_fill) * side_sign / max(risk_per_unit, 1e-12)
            remaining_frac = 1.0 - sum(f for f, _ in open_trade.partials)
            weighted_r = sum(f * r for f, r in open_trade.partials) + remaining_frac * final_r
            risk_dollars = day_start_balance * (config.risk_pct / 100.0)
            balance += weighted_r * risk_dollars
            peak_balance = max(peak_balance, balance)
            open_trade.close_time = bar.time
            open_trade.exit_price = bar.close
            open_trade.exit_reason = "dd_stop"
            open_trade.r_multiple = weighted_r
            open_trade.pnl_r = weighted_r
            trades.append(open_trade)
            open_trade = None
            pending = None

        equity_curve.append((bar.time, equity))

    # Close any position still open at end of data
    if open_trade is not None:
        last = bars[-1]
        side_sign = 1.0 if open_trade.side == "buy" else -1.0
        risk_per_unit = abs(open_trade.entry_fill - open_trade.sl)
        final_r = (last.close - open_trade.entry_fill) * side_sign / max(risk_per_unit, 1e-12)
        remaining_frac = 1.0 - sum(f for f, _ in open_trade.partials)
        weighted_r = sum(f * r for f, r in open_trade.partials) + remaining_frac * final_r
        risk_dollars = day_start_balance * (config.risk_pct / 100.0)
        balance += weighted_r * risk_dollars
        open_trade.close_time = last.time
        open_trade.exit_price = last.close
        open_trade.exit_reason = "end"
        open_trade.r_multiple = weighted_r
        open_trade.pnl_r = weighted_r
        trades.append(open_trade)

    return trades, equity_curve


# --------------------------------- Stats ------------------------------------

@dataclass(frozen=True)
class BacktestStats:
    total_signals: int
    filled: int
    expired: int
    wins: int
    losses: int
    win_rate_pct: float
    avg_r: float
    total_r: float
    best_r: float
    worst_r: float
    max_daily_drawdown_pct: float
    max_overall_drawdown_pct: float
    final_balance: float
    initial_balance: float
    return_pct: float
    dd_stop_triggered: bool


def compute_stats(
    trades: list[SimulatedTrade],
    equity_curve: list[tuple[datetime, float]],
    config: BacktestConfig,
) -> BacktestStats:
    filled = [t for t in trades if t.entry_fill is not None and t.r_multiple is not None]
    expired = [t for t in trades if t.exit_reason == "expired"]
    wins = [t for t in filled if (t.r_multiple or 0.0) > 0]
    losses = [t for t in filled if (t.r_multiple or 0.0) <= 0]
    rs = [t.r_multiple for t in filled if t.r_multiple is not None]

    if equity_curve:
        balances = [e for _, e in equity_curve]
        peak = config.initial_balance
        max_dd = 0.0
        for b in balances:
            peak = max(peak, b)
            dd = 100.0 * (peak - b) / max(peak, 1e-9)
            max_dd = max(max_dd, dd)
        final_balance = balances[-1]
    else:
        max_dd = 0.0
        final_balance = config.initial_balance

    # Max daily DD: compute per-day from equity_curve
    max_daily_dd = 0.0
    by_day: dict[object, list[float]] = {}
    for ts, eq in equity_curve:
        by_day.setdefault(ts.date(), []).append(eq)
    for _, equities in by_day.items():
        if not equities:
            continue
        start_eq = equities[0]
        day_low = min(equities)
        dd = 100.0 * (start_eq - day_low) / max(start_eq, 1e-9)
        max_daily_dd = max(max_daily_dd, dd)

    dd_triggered = any(t.exit_reason == "dd_stop" for t in trades)

    return BacktestStats(
        total_signals=len(trades),
        filled=len(filled),
        expired=len(expired),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=(100.0 * len(wins) / len(filled)) if filled else 0.0,
        avg_r=(sum(rs) / len(rs)) if rs else 0.0,
        total_r=sum(rs) if rs else 0.0,
        best_r=max(rs) if rs else 0.0,
        worst_r=min(rs) if rs else 0.0,
        max_daily_drawdown_pct=round(max_daily_dd, 2),
        max_overall_drawdown_pct=round(max_dd, 2),
        final_balance=round(final_balance, 2),
        initial_balance=config.initial_balance,
        return_pct=round(100.0 * (final_balance - config.initial_balance) / config.initial_balance, 2),
        dd_stop_triggered=dd_triggered,
    )


# --------------------------- Portfolio simulation ---------------------------

@dataclass(frozen=True)
class PortfolioEntry:
    """One (symbol, timeframe, setup) to include in the portfolio."""
    symbol: str
    timeframe: Timeframe
    setup: str                     # "london_breakout" | "ny_momentum" | "gold_pullback" | "failed_breakout_fade"
    pip_size: float


@dataclass(frozen=True)
class PortfolioStats:
    total_trade_attempts: int      # all signals that tried to enter
    filled: int                    # actually taken (passed concurrent + DD gates)
    skipped_concurrent: int
    skipped_dd: int
    wins: int
    losses: int
    win_rate_pct: float
    avg_r_per_trade: float
    total_r: float
    final_balance: float
    initial_balance: float
    return_pct: float
    max_daily_dd_pct: float
    max_overall_dd_pct: float
    dd_hard_stop_hit: bool
    by_setup: dict[str, int]       # count filled per setup
    by_symbol: dict[str, int]      # count filled per symbol


@dataclass(frozen=True)
class PortfolioConfig:
    initial_balance: float = 50_000.0
    risk_pct: float = 0.5
    max_concurrent_positions: int = 3
    daily_dd_hard_stop_pct: float = 4.0
    max_dd_hard_stop_pct: float = 8.0


def _build_detector_for_entry(entry: PortfolioEntry, bars: list[Bar]):
    if entry.setup == "london_breakout":
        return _default_detector
    if entry.setup == "ny_momentum":
        def d(day_bars, all_bars, cfg):
            return detect_ny_momentum(day_bars)
        return d
    if entry.setup == "gold_pullback":
        return make_gold_pullback_detector(bars)
    if entry.setup == "failed_breakout_fade":
        return make_failed_breakout_fade_detector(bars)
    raise ValueError(f"Unknown setup: {entry.setup}")


def run_portfolio(
    entries: list[PortfolioEntry],
    *,
    start: datetime,
    end: datetime,
    config: PortfolioConfig = PortfolioConfig(),
    bar_loader=None,
) -> tuple[list[SimulatedTrade], PortfolioStats, list[tuple[datetime, float]]]:
    """Run every entry independently, then replay their trades globally.

    Uses event-replay: each independent backtest produces a list of trades
    with fill_time, close_time, r_multiple. We sort all filled trades by
    fill_time, walk chronologically, and take each one only if:
      - concurrent positions < max_concurrent_positions, AND
      - daily DD hasn't breached hard stop.

    Skipped trades are discarded (they never took a slot). Taken trades
    apply their pre-computed r_multiple against CURRENT balance at entry.

    Caveats vs a true interleaved-bar simulation:
      - Each setup's r_multiple was computed in isolation, so manage-runners
        and SL/TP hit on EACH trade already. Those outcomes are taken as
        ground truth.
      - We don't model correlation-driven co-movement: if two trades were
        simulated separately and both won, we credit both. If they were
        actually on correlated symbols, real live behavior might differ.
      - Balance compounds correctly: each trade's R is applied to the
        balance AT ENTRY, capturing equity-curve compounding across setups.
    """
    if bar_loader is None:
        bar_loader = lambda sym, tf: load_mt5_history(sym, tf, start, end)

    # Step 1 — run each entry's individual backtest
    all_trades: list[SimulatedTrade] = []
    per_entry_config_for_log: dict[str, BacktestConfig] = {}
    for entry in entries:
        bars = bar_loader(entry.symbol, entry.timeframe)
        bt_cfg = BacktestConfig(
            symbol=entry.symbol,
            initial_balance=config.initial_balance,
            risk_pct=config.risk_pct,
            pip_size=entry.pip_size,
            enable_manage_runners=True,
        )
        detector = _build_detector_for_entry(entry, bars)
        trades, _ = run_backtest(bars, config=bt_cfg, detector=detector)
        all_trades.extend(t for t in trades if t.entry_fill is not None and t.fill_time is not None)
        per_entry_config_for_log[f"{entry.symbol}/{entry.setup}"] = bt_cfg

    # Step 2 — chronological replay with global state
    all_trades.sort(key=lambda t: t.fill_time)

    balance = config.initial_balance
    peak_balance = balance
    equity_curve: list[tuple[datetime, float]] = [(start, balance)]
    concurrent: list[SimulatedTrade] = []   # open positions, sorted by close_time
    taken: list[SimulatedTrade] = []
    skipped_cap = 0
    skipped_dd = 0
    current_day = None
    day_start_balance = balance
    day_lowest_balance = balance
    daily_dd_stop_today = False
    max_daily_dd = 0.0
    max_overall_dd = 0.0
    dd_hit = False

    def _close_expired_up_to(now: datetime) -> None:
        nonlocal balance, peak_balance, concurrent, day_lowest_balance, max_overall_dd
        concurrent.sort(key=lambda t: t.close_time or now)
        while concurrent and (concurrent[0].close_time or now) <= now:
            t = concurrent.pop(0)
            # Realize R against the entry-balance snapshot we stored on t.pnl_r-base
            entry_bal = getattr(t, "_entry_balance", day_start_balance)
            risk_dollars = entry_bal * (config.risk_pct / 100.0)
            balance += (t.r_multiple or 0.0) * risk_dollars
            peak_balance = max(peak_balance, balance)
            day_lowest_balance = min(day_lowest_balance, balance)
            dd_pct = 100.0 * (peak_balance - balance) / max(peak_balance, 1e-9)
            max_overall_dd = max(max_overall_dd, dd_pct)
            equity_curve.append((t.close_time or now, balance))

    for trade in all_trades:
        fill_time = trade.fill_time
        # Realize closes that happened before this entry
        _close_expired_up_to(fill_time)

        # Day rollover
        if current_day != fill_time.date():
            if current_day is not None:
                dd_today = 100.0 * (day_start_balance - day_lowest_balance) / max(day_start_balance, 1e-9)
                max_daily_dd = max(max_daily_dd, dd_today)
            current_day = fill_time.date()
            day_start_balance = balance
            day_lowest_balance = balance
            daily_dd_stop_today = False

        # Daily DD gate
        cur_daily_dd = 100.0 * (day_start_balance - balance) / max(day_start_balance, 1e-9)
        if cur_daily_dd >= config.daily_dd_hard_stop_pct:
            daily_dd_stop_today = True
            dd_hit = True
        cur_overall_dd = 100.0 * (peak_balance - balance) / max(peak_balance, 1e-9)
        if cur_overall_dd >= config.max_dd_hard_stop_pct:
            dd_hit = True

        if daily_dd_stop_today or dd_hit:
            skipped_dd += 1
            continue

        # Concurrent cap
        if len(concurrent) >= config.max_concurrent_positions:
            skipped_cap += 1
            continue

        # Take the trade; stamp the entry balance for later R sizing
        trade._entry_balance = balance  # type: ignore[attr-defined]
        concurrent.append(trade)
        taken.append(trade)
        equity_curve.append((fill_time, balance))

    # Realize remaining positions
    if concurrent:
        last_close = max((t.close_time or end) for t in concurrent)
        _close_expired_up_to(last_close)

    # Final daily DD for the last day
    if current_day is not None:
        dd_today = 100.0 * (day_start_balance - day_lowest_balance) / max(day_start_balance, 1e-9)
        max_daily_dd = max(max_daily_dd, dd_today)

    rs = [t.r_multiple for t in taken if t.r_multiple is not None]
    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r <= 0)
    by_setup: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    for t in taken:
        by_setup[t.setup] = by_setup.get(t.setup, 0) + 1
        by_symbol[t.symbol] = by_symbol.get(t.symbol, 0) + 1

    stats = PortfolioStats(
        total_trade_attempts=len(all_trades),
        filled=len(taken),
        skipped_concurrent=skipped_cap,
        skipped_dd=skipped_dd,
        wins=wins,
        losses=losses,
        win_rate_pct=(100.0 * wins / len(rs)) if rs else 0.0,
        avg_r_per_trade=(sum(rs) / len(rs)) if rs else 0.0,
        total_r=sum(rs) if rs else 0.0,
        final_balance=round(balance, 2),
        initial_balance=config.initial_balance,
        return_pct=round(100.0 * (balance - config.initial_balance) / config.initial_balance, 2),
        max_daily_dd_pct=round(max_daily_dd, 2),
        max_overall_dd_pct=round(max_overall_dd, 2),
        dd_hard_stop_hit=dd_hit,
        by_setup=by_setup,
        by_symbol=by_symbol,
    )
    return taken, stats, equity_curve


def format_portfolio_report(stats: PortfolioStats, taken: list[SimulatedTrade]) -> str:
    lines = [
        "=" * 72,
        "  PORTFOLIO BACKTEST REPORT",
        "=" * 72,
        "",
        f"  Initial: ${stats.initial_balance:,.2f}   Final: ${stats.final_balance:,.2f}  ({stats.return_pct:+.2f}%)",
        "",
        "  FLOW",
        f"    Total signal attempts:  {stats.total_trade_attempts}",
        f"    Filled (taken):         {stats.filled}",
        f"    Skipped (cap >=3 open): {stats.skipped_concurrent}",
        f"    Skipped (DD hard stop): {stats.skipped_dd}",
        "",
        "  OUTCOMES (taken trades)",
        f"    Wins:      {stats.wins}",
        f"    Losses:    {stats.losses}",
        f"    Win rate:  {stats.win_rate_pct:.1f}%",
        f"    Avg R:     {stats.avg_r_per_trade:+.3f}",
        f"    Total R:   {stats.total_r:+.2f}",
        "",
        "  RISK",
        f"    Max daily DD:     {stats.max_daily_dd_pct:.2f}%",
        f"    Max overall DD:   {stats.max_overall_dd_pct:.2f}%",
        f"    DD hard stop hit: {stats.dd_hard_stop_hit}",
        "",
        "  BY SETUP",
    ]
    for s, n in sorted(stats.by_setup.items(), key=lambda x: -x[1]):
        lines.append(f"    {s:30s} {n:4d}")
    lines.append("")
    lines.append("  BY SYMBOL")
    for s, n in sorted(stats.by_symbol.items(), key=lambda x: -x[1]):
        lines.append(f"    {s:30s} {n:4d}")
    lines.append("=" * 72)
    return "\n".join(lines)


DEFAULT_PORTFOLIO_ENTRIES: list[PortfolioEntry] = [
    # FX Failed-Breakout Fade on H4 — anchor of the strategy
    PortfolioEntry("EURUSD", Timeframe.H4, "failed_breakout_fade", 0.0001),
    PortfolioEntry("GBPUSD", Timeframe.H4, "failed_breakout_fade", 0.0001),
    PortfolioEntry("USDJPY", Timeframe.H4, "failed_breakout_fade", 0.01),
    PortfolioEntry("AUDUSD", Timeframe.H4, "failed_breakout_fade", 0.0001),
    # Gold Trend Pullback on H1
    PortfolioEntry("XAUUSD", Timeframe.H1, "gold_pullback", 0.01),
    # NY Open Momentum on winners only (drop US30 — backtest-negative)
    PortfolioEntry("USTEC", Timeframe.M15, "ny_momentum", 1.0),
    PortfolioEntry("US500", Timeframe.M15, "ny_momentum", 1.0),
    # London Breakout on EURUSD M15 — low-expectancy, kept for diversification
    PortfolioEntry("EURUSD", Timeframe.M15, "london_breakout", 0.0001),
]


# --------------------------------- Report -----------------------------------

def format_report(stats: BacktestStats, trades: list[SimulatedTrade], config: BacktestConfig) -> str:
    setup_name = trades[0].setup if trades else "?"
    lines = [
        "=" * 72,
        f"  BACKTEST REPORT -- {config.symbol} / {setup_name}",
        "=" * 72,
        "",
        f"  Period:        {trades[0].signal_time.date() if trades else '-'}"
        f" -> {trades[-1].close_time.date() if trades and trades[-1].close_time else '-'}",
        f"  Initial:       ${config.initial_balance:,.2f}",
        f"  Final:         ${stats.final_balance:,.2f}  ({stats.return_pct:+.2f}%)",
        "",
        "  SIGNALS / FILLS",
        f"    Total signals:   {stats.total_signals}",
        f"    Filled:          {stats.filled}",
        f"    Expired (no fill): {stats.expired}",
        "",
        "  FILLED TRADE OUTCOMES",
        f"    Wins:            {stats.wins}",
        f"    Losses:          {stats.losses}",
        f"    Win rate:        {stats.win_rate_pct:.1f}%",
        f"    Avg R:           {stats.avg_r:+.3f}",
        f"    Total R:         {stats.total_r:+.2f}",
        f"    Best / Worst:    {stats.best_r:+.2f}R / {stats.worst_r:+.2f}R",
        "",
        "  RISK",
        f"    Max daily DD:    {stats.max_daily_drawdown_pct:.2f}%",
        f"    Max overall DD:  {stats.max_overall_drawdown_pct:.2f}%",
        f"    DD hard stop hit: {stats.dd_stop_triggered}",
        "",
        f"  MANAGE-RUNNERS:  {'ENABLED' if config.enable_manage_runners else 'DISABLED (Phase 1 baseline)'}",
        "",
        "  LIMITATIONS (still present)",
        "    - No news filter -- red-folder events not excluded",
        "    - LLM rubric absent -- items 2/3/5 forced true",
        "    - Single position at a time (real cap is 3 concurrent)",
        "    - Fixed 0.5% risk per trade (B-grade); no A-grade sizing",
        "=" * 72,
    ]
    return "\n".join(lines)


def save_results(output_dir: Path, stats: BacktestStats, trades: list[SimulatedTrade], config: BacktestConfig) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "stats.json").open("w", encoding="utf-8") as f:
        json.dump({"stats": asdict(stats), "config": asdict(config)}, f, indent=2, default=str)
    with (output_dir / "trades.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "signal_time", "fill_time", "close_time", "side", "entry_planned",
            "entry_fill", "sl", "tp", "exit_price", "exit_reason", "r_multiple",
        ])
        for t in trades:
            writer.writerow([
                t.signal_time.isoformat(),
                t.fill_time.isoformat() if t.fill_time else "",
                t.close_time.isoformat() if t.close_time else "",
                t.side,
                t.entry_planned,
                t.entry_fill if t.entry_fill is not None else "",
                t.sl, t.tp,
                t.exit_price if t.exit_price is not None else "",
                t.exit_reason or "",
                f"{t.r_multiple:.4f}" if t.r_multiple is not None else "",
            ])


# ---------------------------------- CLI -------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.backtest")
    parser.add_argument("--symbol", default="EURUSD", help="default: EURUSD")
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--balance", type=float, default=50_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.5)
    parser.add_argument("--min-range-pips", type=float, default=20.0)
    parser.add_argument("--output", help="Dir for CSV + JSON results")
    parser.add_argument("--json", action="store_true", help="Print stats as JSON instead of text report")
    parser.add_argument(
        "--no-manage-runners", action="store_true",
        help="Disable partial TPs + trailing SL (Phase 1 baseline behavior)",
    )
    parser.add_argument(
        "--setup",
        choices=["london_breakout", "ny_momentum", "gold_pullback", "failed_breakout_fade"],
        default="london_breakout",
        help="Which playbook setup to backtest",
    )
    parser.add_argument(
        "--timeframe", choices=["M15", "H1", "H4"], default="M15",
        help="Bar timeframe (default M15; gold_pullback typically uses H1)",
    )
    parser.add_argument(
        "--pip-size", type=float,
        help="Override pip size (default 0.0001 for FX, 1.0 for indices, 0.01 for XAUUSD)",
    )
    parser.add_argument(
        "--portfolio", action="store_true",
        help="Run the full portfolio (all 4 playbook setups across 7 symbols)",
    )
    args = parser.parse_args(argv)

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    if args.portfolio:
        pcfg = PortfolioConfig(initial_balance=args.balance, risk_pct=args.risk_pct)
        print(f"[backtest] running portfolio simulation ({len(DEFAULT_PORTFOLIO_ENTRIES)} entries)...", file=sys.stderr)
        taken, pstats, equity = run_portfolio(
            DEFAULT_PORTFOLIO_ENTRIES, start=start, end=end, config=pcfg,
        )
        if args.json:
            print(json.dumps({"stats": asdict(pstats), "n_taken": len(taken)}, indent=2, default=str))
        else:
            print(format_portfolio_report(pstats, taken))
        if args.output:
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=True)
            with (out / "portfolio_stats.json").open("w", encoding="utf-8") as f:
                json.dump(asdict(pstats), f, indent=2, default=str)
            with (out / "portfolio_trades.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["fill_time", "close_time", "symbol", "side", "setup", "r_multiple", "entry_balance"])
                for t in taken:
                    w.writerow([
                        t.fill_time.isoformat() if t.fill_time else "",
                        t.close_time.isoformat() if t.close_time else "",
                        t.symbol, t.side, t.setup,
                        f"{t.r_multiple:.4f}" if t.r_multiple is not None else "",
                        f"{getattr(t, '_entry_balance', 0):.2f}",
                    ])
            print(f"[backtest] portfolio results saved to {out.resolve()}", file=sys.stderr)
        return 0

    # Sensible pip defaults per instrument class
    if args.symbol in {"US30", "NAS100", "SPX500", "GER40", "USTEC", "US500"}:
        default_pip = 1.0      # index points
    elif args.symbol.startswith("XAU"):
        default_pip = 0.01     # gold centi-dollar
    elif "JPY" in args.symbol:
        default_pip = 0.01     # JPY pairs quote with 3 decimals
    else:
        default_pip = 0.0001   # FX majors
    pip = args.pip_size if args.pip_size is not None else default_pip

    config = BacktestConfig(
        symbol=args.symbol,
        initial_balance=args.balance,
        risk_pct=args.risk_pct,
        min_asian_range_pips=args.min_range_pips,
        pip_size=pip,
        enable_manage_runners=not args.no_manage_runners,
    )

    tf = Timeframe[args.timeframe]
    print(f"[backtest] loading {args.symbol} {args.timeframe} bars from MT5 "
          f"({start.date()} -> {end.date()})...", file=sys.stderr)
    bars = load_mt5_history(args.symbol, tf, start, end)
    print(f"[backtest] loaded {len(bars)} {args.timeframe} bars", file=sys.stderr)

    if args.setup == "london_breakout":
        detector = _default_detector
    elif args.setup == "ny_momentum":
        def detector(day_bars, all_bars, cfg):
            return detect_ny_momentum(day_bars)
    elif args.setup == "gold_pullback":
        detector = make_gold_pullback_detector(bars)
    else:  # failed_breakout_fade
        detector = make_failed_breakout_fade_detector(bars)

    print(f"[backtest] running simulation (setup={args.setup})...", file=sys.stderr)
    trades, equity_curve = run_backtest(bars, config=config, detector=detector)
    stats = compute_stats(trades, equity_curve, config)

    if args.json:
        print(json.dumps({"stats": asdict(stats), "config": asdict(config)}, indent=2, default=str))
    else:
        print(format_report(stats, trades, config))

    if args.output:
        out = Path(args.output)
        save_results(out, stats, trades, config)
        print(f"[backtest] results saved to {out.resolve()}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

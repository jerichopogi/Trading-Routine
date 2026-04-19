"""Guardrail enforcement — the critical tests.

Every scenario here represents a way to blow a FundedNext account. The
guardrail module MUST reject them before they reach the broker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.account import EquitySnapshot, append_snapshot
from scripts.broker import OrderRequest, OrderSide
from scripts.broker.mock_broker import MockBroker
from scripts.guardrails import check_or_reject

# Wednesday 10:00 UTC — FX + indices open, not near Friday flatten.
WED_10_UTC = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)


def _compliant_order(symbol: str = "EURUSD", volume: float = 0.1) -> OrderRequest:
    # 30 pip stop, 0.1 lots on EURUSD = ~$30 = 0.06% risk on $50k — well under 0.5%.
    return OrderRequest(
        symbol=symbol, side=OrderSide.BUY, volume=volume,
        sl=1.07200, tp=1.07800, comment="test",
    )


def test_compliant_order_passes(mock_broker: MockBroker) -> None:
    verdict = check_or_reject(_compliant_order(), broker=mock_broker, now=WED_10_UTC)
    assert verdict.ok, verdict.reasons


def test_non_whitelisted_symbol_rejected(mock_broker: MockBroker) -> None:
    mock_broker.set_price("BTCUSD", bid=50000.0, ask=50005.0)
    order = OrderRequest(
        symbol="BTCUSD", side=OrderSide.BUY, volume=0.01,
        sl=49500.0, tp=51000.0, comment="crypto",
    )
    verdict = check_or_reject(order, broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("whitelist" in r for r in verdict.reasons)


def test_missing_stop_loss_rejected(mock_broker: MockBroker) -> None:
    order = OrderRequest(symbol="EURUSD", side=OrderSide.BUY, volume=0.1)
    verdict = check_or_reject(order, broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("stop loss" in r for r in verdict.reasons)


def test_excessive_per_trade_risk_rejected(mock_broker: MockBroker) -> None:
    # 5 lots * 100000 * 0.003 = $1500 → 3% risk on $50k, way over 0.5%.
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=5.0,
        sl=1.07200, tp=1.08000,
    )
    verdict = check_or_reject(order, broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("risk" in r.lower() for r in verdict.reasons)


def test_fourth_concurrent_position_rejected(mock_broker: MockBroker) -> None:
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        mock_broker.place_order(OrderRequest(
            symbol=sym, side=OrderSide.BUY, volume=0.1,
            sl=0.5, tp=2.0,  # sl far enough for test purposes
        ))
    new = _compliant_order("AUDUSD")
    verdict = check_or_reject(new, broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("open" in r and "cap" in r for r in verdict.reasons)


def test_daily_dd_hard_stop_blocks_new_orders(
    mock_broker: MockBroker, isolated_memory
) -> None:
    # Seed equity curve so start-of-day equity is 50000 but current equity is 47700 (4.6%).
    sod = WED_10_UTC.replace(hour=22, minute=0) - timedelta(days=1)
    append_snapshot(EquitySnapshot(ts=sod, balance=50000.0, equity=50000.0,
                                   open_profit=0.0, stage="demo"))
    # Simulate account equity after a drawdown
    mock_broker.set_balance(47700.0)
    verdict = check_or_reject(_compliant_order(), broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("daily DD" in r for r in verdict.reasons)


def test_max_dd_hard_stop_blocks(mock_broker: MockBroker, isolated_memory) -> None:
    mock_broker.set_balance(45000.0)  # 10% down — way past 8% internal
    verdict = check_or_reject(_compliant_order(), broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("max DD" in r for r in verdict.reasons)


def test_weekend_flatten_window_blocks(mock_broker: MockBroker) -> None:
    friday_late = datetime(2026, 4, 24, 21, 30, tzinfo=UTC)  # Fri 21:30 UTC
    verdict = check_or_reject(_compliant_order(), broker=mock_broker, now=friday_late)
    assert not verdict.ok
    assert any("Weekend" in r or "Friday" in r for r in verdict.reasons)


def test_closed_session_blocks_index(mock_broker: MockBroker) -> None:
    # Sunday 23:00 UTC — indices closed
    sunday_night = datetime(2026, 4, 26, 23, 0, tzinfo=UTC)
    order = OrderRequest(
        symbol="NAS100", side=OrderSide.BUY, volume=0.1,
        sl=17000.0, tp=17600.0,
    )
    verdict = check_or_reject(order, broker=mock_broker, now=sunday_night)
    assert not verdict.ok
    assert any("session" in r.lower() for r in verdict.reasons)


def test_max_concurrent_or_per_day_blocks(mock_broker: MockBroker) -> None:
    """3 open positions — attempting a 4th must be blocked (by concurrent cap)."""
    mock_broker.set_clock(WED_10_UTC)
    for sym in ("EURUSD", "GBPUSD", "USDJPY"):
        mock_broker.place_order(OrderRequest(
            symbol=sym, side=OrderSide.BUY, volume=0.01, sl=0.5, tp=2.0,
        ))
    verdict = check_or_reject(_compliant_order("NZDUSD"), broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("open" in r or "today" in r for r in verdict.reasons)


def test_zero_volume_rejected(mock_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.0,
        sl=1.07200, tp=1.07800,
    )
    verdict = check_or_reject(order, broker=mock_broker, now=WED_10_UTC)
    assert not verdict.ok
    assert any("volume" in r.lower() for r in verdict.reasons)


def test_verdict_metadata_includes_dd_percentages(
    mock_broker: MockBroker, isolated_memory
) -> None:
    verdict = check_or_reject(_compliant_order(), broker=mock_broker, now=WED_10_UTC)
    assert "daily_dd_pct" in verdict.metadata
    assert "max_dd_pct" in verdict.metadata
    assert "risk_pct_limit" in verdict.metadata

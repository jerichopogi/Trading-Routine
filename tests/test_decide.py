"""Decision-layer helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from scripts import decide
from scripts.broker import OrderSide
from scripts.broker.mock_broker import MockBroker

WED_10_UTC = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)


def test_size_by_risk_basic() -> None:
    lots = decide.size_by_risk(
        symbol="EURUSD",
        entry=1.08000, stop=1.07700,   # 30 pips = 0.0030
        balance=50000.0,
        risk_pct=0.5,                   # $250 risk
        contract_size=100000,
    )
    # 250 / (0.003 * 100000) = 0.833 lots
    assert abs(lots - 0.83) < 0.01


def test_size_by_risk_zero_distance() -> None:
    lots = decide.size_by_risk(
        symbol="EURUSD", entry=1.08, stop=1.08,
        balance=50000.0, risk_pct=0.5, contract_size=100000,
    )
    assert lots == 0.0


def test_draft_order_sizes_correctly(mock_broker: MockBroker) -> None:
    order = decide.draft_order(
        symbol="EURUSD",
        side=OrderSide.BUY,
        entry=1.07500,
        stop=1.07200,
        target=1.08100,
        broker=mock_broker,
        comment="intraday-london-breakout",
    )
    assert order.symbol == "EURUSD"
    assert order.side == OrderSide.BUY
    assert order.sl == 1.07200
    assert order.tp == 1.08100
    assert order.volume > 0
    # Risk should be ≤ 0.5% of balance = $250. Distance = 0.003, contract=100000.
    # loss_at_stop = volume * 0.003 * 100000
    loss = order.volume * 0.003 * 100000
    assert loss <= 250 + 1e-3


def test_preflight_reports_state(mock_broker: MockBroker) -> None:
    report = decide.preflight(mock_broker, stage="demo", now=WED_10_UTC)
    assert report.stage == "demo"
    assert report.balance == 50000.0
    assert report.equity == 50000.0
    assert not report.firm_violation
    assert not report.hard_stop_hit
    # At Wed 10:00 UTC, FX sessions should be open
    assert "EURUSD" in report.tradeable_symbols

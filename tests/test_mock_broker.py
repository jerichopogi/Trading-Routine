"""MockBroker sanity tests."""

from __future__ import annotations

from scripts.broker import OrderRequest, OrderSide, Timeframe
from scripts.broker.mock_broker import MockBroker


def test_account_info_defaults(mock_broker: MockBroker) -> None:
    info = mock_broker.account_info()
    assert info.balance == 50000.0
    assert info.equity == 50000.0
    assert info.currency == "USD"


def test_place_and_modify_order(mock_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.07000, tp=1.08000, comment="t1",
    )
    res = mock_broker.place_order(order)
    assert res.ok
    assert res.ticket is not None
    positions = mock_broker.positions()
    assert len(positions) == 1
    assert positions[0].sl == 1.07000

    assert mock_broker.modify_position(res.ticket, sl=1.07200)
    assert mock_broker.positions()[0].sl == 1.07200


def test_price_move_realizes_profit(mock_broker: MockBroker) -> None:
    mock_broker.place_order(OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1, sl=1.07000, tp=1.08000,
    ))
    # Move price up
    mock_broker.set_price("EURUSD", bid=1.07800, ask=1.07802)
    p = mock_broker.positions()[0]
    # 0.1 lots * 100000 * (1.07800 - 1.07502) ≈ $29.80 — contract size math
    assert p.profit > 0


def test_close_position_updates_balance(mock_broker: MockBroker) -> None:
    mock_broker.place_order(OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1, sl=1.07000, tp=1.08000,
    ))
    mock_broker.set_price("EURUSD", bid=1.07800, ask=1.07802)
    before = mock_broker.account_info().balance
    closed = mock_broker.close_position(mock_broker.positions()[0].ticket)
    assert closed
    after = mock_broker.account_info().balance
    assert after > before
    assert len(mock_broker.positions()) == 0


def test_rates_returns_requested_count(mock_broker: MockBroker) -> None:
    bars = mock_broker.rates("EURUSD", Timeframe.M15, 10)
    assert len(bars) == 10
    assert bars[0].time < bars[-1].time


def test_unknown_symbol_rejects_order(mock_broker: MockBroker) -> None:
    res = mock_broker.place_order(OrderRequest(
        symbol="XXXYYY", side=OrderSide.BUY, volume=0.1,
    ))
    assert not res.ok

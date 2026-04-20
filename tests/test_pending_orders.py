"""Pending order path — MockBroker submits, stores, cancels, and auto-fills on price crossings.

Covers the four pending types (BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP)
plus the `draft_order` auto-classification of MARKET vs LIMIT vs STOP based
on the gap between `entry` and the live bid/ask.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts import decide, trade
from scripts.broker import OrderRequest, OrderSide
from scripts.broker.base import OrderKind
from scripts.broker.mock_broker import MockBroker


@pytest.fixture
def eurusd_broker(mock_broker: MockBroker) -> MockBroker:
    mock_broker.set_price("EURUSD", bid=1.17500, ask=1.17502)
    return mock_broker


# ---------- MockBroker pending storage ----------


def test_pending_buy_limit_stored_not_filled(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17100, tp=1.18000,
        kind=OrderKind.LIMIT, entry=1.17400,
    )
    result = eurusd_broker.place_order(order)
    assert result.ok
    assert result.ticket is not None
    assert len(eurusd_broker.pending_orders()) == 1
    assert len(eurusd_broker.positions()) == 0  # not filled yet


def test_pending_buy_limit_auto_fills_on_price_drop(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17100, tp=1.18000,
        kind=OrderKind.LIMIT, entry=1.17400,
    )
    eurusd_broker.place_order(order)
    # ask drops to at-or-below entry — LIMIT should fill
    eurusd_broker.set_price("EURUSD", bid=1.17398, ask=1.17400)
    assert len(eurusd_broker.pending_orders()) == 0
    assert len(eurusd_broker.positions()) == 1
    pos = eurusd_broker.positions()[0]
    assert pos.side == OrderSide.BUY
    assert pos.sl == 1.17100
    assert pos.tp == 1.18000


def test_pending_buy_limit_does_not_fill_when_ask_above(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17100, tp=1.18000,
        kind=OrderKind.LIMIT, entry=1.17400,
    )
    eurusd_broker.place_order(order)
    # bid below entry but ask still above — must NOT fill
    eurusd_broker.set_price("EURUSD", bid=1.17399, ask=1.17401)
    assert len(eurusd_broker.pending_orders()) == 1
    assert len(eurusd_broker.positions()) == 0


def test_pending_buy_stop_fills_on_breakout(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17400, tp=1.18200,
        kind=OrderKind.STOP, entry=1.17700,
    )
    eurusd_broker.place_order(order)
    eurusd_broker.set_price("EURUSD", bid=1.17699, ask=1.17701)
    assert len(eurusd_broker.positions()) == 1


def test_pending_sell_limit_fills_on_rally(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.SELL, volume=0.1,
        sl=1.18200, tp=1.17400,
        kind=OrderKind.LIMIT, entry=1.17900,
    )
    eurusd_broker.place_order(order)
    # bid rallies to entry — SELL LIMIT fills when bid >= entry
    eurusd_broker.set_price("EURUSD", bid=1.17900, ask=1.17902)
    assert len(eurusd_broker.positions()) == 1
    assert eurusd_broker.positions()[0].side == OrderSide.SELL


def test_pending_sell_stop_fills_on_breakdown(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.SELL, volume=0.1,
        sl=1.17700, tp=1.17000,
        kind=OrderKind.STOP, entry=1.17300,
    )
    eurusd_broker.place_order(order)
    eurusd_broker.set_price("EURUSD", bid=1.17300, ask=1.17302)
    assert len(eurusd_broker.positions()) == 1


def test_cancel_pending_order_removes_it(eurusd_broker: MockBroker) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17100, tp=1.18000,
        kind=OrderKind.LIMIT, entry=1.17400,
    )
    result = eurusd_broker.place_order(order)
    assert eurusd_broker.cancel_pending_order(result.ticket) is True
    assert len(eurusd_broker.pending_orders()) == 0


def test_cancel_pending_unknown_returns_false(eurusd_broker: MockBroker) -> None:
    assert eurusd_broker.cancel_pending_order(999_999) is False


# ---------- trade.cancel_all_pending ----------


def test_cancel_all_pending_clears_all(eurusd_broker: MockBroker) -> None:
    for entry in (1.17400, 1.17300, 1.17200):
        eurusd_broker.place_order(OrderRequest(
            symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
            sl=entry - 0.002, tp=entry + 0.006,
            kind=OrderKind.LIMIT, entry=entry,
        ))
    assert len(eurusd_broker.pending_orders()) == 3
    with patch("scripts.trade.notify"):
        cancelled = trade.cancel_all_pending(eurusd_broker, reason="test")
    assert len(cancelled) == 3
    assert len(eurusd_broker.pending_orders()) == 0


def test_flatten_all_also_cancels_pending(eurusd_broker: MockBroker) -> None:
    # one open position (via market) + one pending
    eurusd_broker.place_order(OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17300, tp=1.17700, kind=OrderKind.MARKET,
    ))
    eurusd_broker.place_order(OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=1.17100, tp=1.18000, kind=OrderKind.LIMIT, entry=1.17400,
    ))
    assert len(eurusd_broker.positions()) == 1
    assert len(eurusd_broker.pending_orders()) == 1

    with patch("scripts.trade.notify"):
        closed = trade.flatten_all(eurusd_broker, reason="EOD", stage="dev")
    assert len(closed) == 1
    assert len(eurusd_broker.positions()) == 0
    assert len(eurusd_broker.pending_orders()) == 0  # pending also cancelled


# ---------- draft_order auto-classification ----------


def test_draft_order_at_market_price_is_MARKET(eurusd_broker: MockBroker) -> None:
    # entry ~= ask → market
    order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.BUY,
        entry=1.17502, stop=1.17200, target=1.18000,
        broker=eurusd_broker, grade=decide.ConvictionGrade.B,
    )
    assert order.kind == OrderKind.MARKET


def test_draft_order_below_bid_buy_is_LIMIT(eurusd_broker: MockBroker) -> None:
    order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.BUY,
        entry=1.17400, stop=1.17100, target=1.18000,
        broker=eurusd_broker, grade=decide.ConvictionGrade.B,
    )
    assert order.kind == OrderKind.LIMIT
    assert order.entry == 1.17400


def test_draft_order_above_ask_buy_is_STOP(eurusd_broker: MockBroker) -> None:
    order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.BUY,
        entry=1.17700, stop=1.17400, target=1.18200,
        broker=eurusd_broker, grade=decide.ConvictionGrade.B,
    )
    assert order.kind == OrderKind.STOP


def test_draft_order_above_ask_sell_is_LIMIT(eurusd_broker: MockBroker) -> None:
    order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.SELL,
        entry=1.17900, stop=1.18200, target=1.17400,
        broker=eurusd_broker, grade=decide.ConvictionGrade.B,
    )
    assert order.kind == OrderKind.LIMIT


def test_draft_order_below_bid_sell_is_STOP(eurusd_broker: MockBroker) -> None:
    order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.SELL,
        entry=1.17300, stop=1.17600, target=1.17000,
        broker=eurusd_broker, grade=decide.ConvictionGrade.B,
    )
    assert order.kind == OrderKind.STOP

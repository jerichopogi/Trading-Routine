"""Phase 1 trade management — breakeven, partial TPs, step trailing SL.

The rule table (from scripts/management.py):
  r >= 1R and SL not at entry    -> SL to entry (breakeven)
  r >= 2R and no partial yet     -> close 50% + SL to +1R
  r >= 3R and partial <= 70%     -> close another 20% + SL to +2R
  r >  3R (after partials)       -> trail SL 1R behind current; never move back

Idempotency: re-running at the same price is a no-op.
"""

from __future__ import annotations

import pytest

from scripts import journal, management
from scripts.broker import OrderRequest, OrderSide
from scripts.broker.base import OrderKind
from scripts.broker.mock_broker import MockBroker


def _place_and_journal(broker: MockBroker, order: OrderRequest):
    """Mirror what trade.place() does: broker fills, journal records the order."""
    result = broker.place_order(order)
    assert result.ok
    journal.log_order(order=order, result=result, stage="test")
    return result


@pytest.fixture
def long_eurusd(mock_broker: MockBroker) -> tuple[MockBroker, int]:
    """1 lot EURUSD BUY at 1.17500, SL 1.17300 (20 pip risk = 1R)."""
    mock_broker.set_price("EURUSD", bid=1.17500, ask=1.17502)
    result = _place_and_journal(mock_broker, OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=1.00,
        sl=1.17300, tp=1.18000, kind=OrderKind.MARKET,
    ))
    return mock_broker, result.ticket


# ---------- partial close broker primitive ----------


def test_partial_close_reduces_volume(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    assert broker.partial_close_position(ticket, volume_to_close=0.40) is True
    pos = broker.positions()[0]
    assert pos.volume == pytest.approx(0.60, abs=1e-9)


def test_partial_close_full_volume_closes_position(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    assert broker.partial_close_position(ticket, volume_to_close=1.00) is True
    assert len(broker.positions()) == 0


def test_partial_close_more_than_open_is_capped(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    assert broker.partial_close_position(ticket, volume_to_close=5.00) is True
    assert len(broker.positions()) == 0


def test_partial_close_unknown_ticket_returns_false(mock_broker: MockBroker) -> None:
    assert mock_broker.partial_close_position(999_999, volume_to_close=0.1) is False


# ---------- manage_runners rule table ----------


_ORIG_RISK = 0.00202  # 1.17502 entry − 1.17300 SL from the fixture


def _set_long_r(broker: MockBroker, ticket: int, r: float) -> None:
    """Move EURUSD price so the long position sits at +r R (original risk)."""
    pos = next(p for p in broker.positions() if p.ticket == ticket)
    target = pos.price_open + r * _ORIG_RISK
    broker.set_price("EURUSD", bid=target, ask=target + 0.00002)


def test_manage_below_1r_noop(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    _set_long_r(broker, ticket, 0.5)
    report = management.manage_runners(broker)
    assert report.breakevens_moved == 0
    assert report.partials_taken == 0
    assert report.trails_updated == 0
    pos = broker.positions()[0]
    assert pos.sl == 1.17300
    assert pos.volume == pytest.approx(1.00)


def test_manage_at_1r_moves_breakeven(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    _set_long_r(broker, ticket, 1.0)
    report = management.manage_runners(broker)
    assert report.breakevens_moved == 1
    pos = broker.positions()[0]
    assert pos.sl == pytest.approx(pos.price_open, abs=1e-9)
    assert pos.volume == pytest.approx(1.00)  # no partial at 1R


def test_manage_at_2r_takes_50pct_and_sl_to_1r(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    _set_long_r(broker, ticket, 2.0)
    report = management.manage_runners(broker)
    assert report.partials_taken == 1
    pos = broker.positions()[0]
    assert pos.volume == pytest.approx(0.50, abs=1e-9)
    # SL now at entry + 1R
    expected_sl = pos.price_open + 1.0 * (pos.price_open - 1.17300)
    assert pos.sl == pytest.approx(expected_sl, abs=1e-7)


def test_manage_at_3r_takes_20pct_more_and_sl_to_2r(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    # First pass: 2R takes 50%
    _set_long_r(broker, ticket, 2.0)
    management.manage_runners(broker)
    assert broker.positions()[0].volume == pytest.approx(0.50, abs=1e-9)

    # Second pass: 3R takes another 20%
    _set_long_r(broker, ticket, 3.0)
    report = management.manage_runners(broker)
    assert report.partials_taken == 1
    pos = broker.positions()[0]
    # 0.50 volume, close 20% of ORIGINAL (so 0.20), leaves 0.30
    assert pos.volume == pytest.approx(0.30, abs=1e-9)
    expected_sl = pos.price_open + 2.0 * (pos.price_open - 1.17300)
    assert pos.sl == pytest.approx(expected_sl, abs=1e-7)


def test_manage_above_3r_trails_sl_one_r_behind(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    # Walk through 2R then 3R partials
    _set_long_r(broker, ticket, 2.0)
    management.manage_runners(broker)
    _set_long_r(broker, ticket, 3.0)
    management.manage_runners(broker)

    # Now at 5R — SL should trail to 4R (1R behind price)
    _set_long_r(broker, ticket, 5.0)
    report = management.manage_runners(broker)
    assert report.trails_updated == 1
    pos = broker.positions()[0]
    risk = pos.price_open - 1.17300
    expected_sl = pos.price_open + 4.0 * risk
    assert pos.sl == pytest.approx(expected_sl, abs=1e-7)


def test_manage_never_moves_sl_backward(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    _set_long_r(broker, ticket, 5.0)
    management.manage_runners(broker)  # trail to 4R
    pos_after_5r = broker.positions()[0]
    sl_at_5r = pos_after_5r.sl

    # Price pulls back to 4.5R — SL should NOT loosen
    _set_long_r(broker, ticket, 4.5)
    management.manage_runners(broker)
    pos_after_pullback = broker.positions()[0]
    assert pos_after_pullback.sl == pytest.approx(sl_at_5r, abs=1e-9)


def test_manage_is_idempotent(long_eurusd: tuple[MockBroker, int]) -> None:
    broker, ticket = long_eurusd
    _set_long_r(broker, ticket, 2.0)
    management.manage_runners(broker)  # first pass: breakeven + partial
    snapshot = (broker.positions()[0].volume, broker.positions()[0].sl)

    # Re-run at same price — should be no-op
    report = management.manage_runners(broker)
    assert report.breakevens_moved == 0
    assert report.partials_taken == 0
    assert report.trails_updated == 0
    pos = broker.positions()[0]
    assert (pos.volume, pos.sl) == snapshot


def test_manage_handles_short_position(mock_broker: MockBroker) -> None:
    mock_broker.set_price("GBPUSD", bid=1.35000, ask=1.35003)
    _place_and_journal(mock_broker, OrderRequest(
        symbol="GBPUSD", side=OrderSide.SELL, volume=1.00,
        sl=1.35200, tp=1.34400, kind=OrderKind.MARKET,
    ))

    pos = mock_broker.positions()[0]
    risk = abs(pos.price_open - pos.sl)
    # Move to +2R for a short means price DROPS 2R
    target = pos.price_open - 2.0 * risk
    mock_broker.set_price("GBPUSD", bid=target - 0.00002, ask=target)

    report = management.manage_runners(mock_broker)
    assert report.partials_taken == 1
    p = mock_broker.positions()[0]
    assert p.volume == pytest.approx(0.50, abs=1e-9)
    # Short's SL at +1R = entry - 1R
    expected_sl = pos.price_open - 1.0 * risk
    assert p.sl == pytest.approx(expected_sl, abs=1e-7)


def test_manage_no_positions_is_noop(mock_broker: MockBroker) -> None:
    report = management.manage_runners(mock_broker)
    assert report.breakevens_moved == 0
    assert report.partials_taken == 0
    assert report.trails_updated == 0
    assert report.positions_touched == 0


def test_manage_skips_position_without_sl(mock_broker: MockBroker) -> None:
    mock_broker.set_price("EURUSD", bid=1.17500, ask=1.17502)
    mock_broker.place_order(OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        sl=None, tp=None, kind=OrderKind.MARKET,
    ))
    mock_broker.set_price("EURUSD", bid=1.18500, ask=1.18502)
    report = management.manage_runners(mock_broker)
    assert report.positions_touched == 0  # no SL = no R math possible

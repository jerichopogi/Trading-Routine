"""Decision-layer helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from scripts import decide
from scripts.broker import OrderSide
from scripts.broker.mock_broker import MockBroker
from scripts.decide import ConvictionGrade, SetupRubric

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


def test_draft_order_b_grade_uses_default_risk(mock_broker: MockBroker) -> None:
    """B grade (default) sizes to 0.5% on EURUSD."""
    order = decide.draft_order(
        symbol="EURUSD",
        side=OrderSide.BUY,
        entry=1.07500,
        stop=1.07200,
        target=1.08100,
        broker=mock_broker,
        comment="intraday-london-breakout",
    )
    assert order.volume > 0
    # Risk ≤ 0.5% of $50k = $250. Distance=0.003, contract=100000.
    loss = order.volume * 0.003 * 100000
    assert loss <= 250 + 1e-3
    # Comment should carry grade
    assert "B" in order.comment


def test_draft_order_a_grade_sizes_up_to_ceiling(mock_broker: MockBroker) -> None:
    """A grade (high conviction) sizes to 1.0% on EURUSD — 2x the B grade volume."""
    b_order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.BUY,
        entry=1.07500, stop=1.07200, target=1.08100,
        broker=mock_broker, grade=ConvictionGrade.B,
    )
    a_order = decide.draft_order(
        symbol="EURUSD", side=OrderSide.BUY,
        entry=1.07500, stop=1.07200, target=1.08100,
        broker=mock_broker, grade=ConvictionGrade.A,
    )
    # A grade should be ~2x B grade (tolerance for rounding to 2 decimals)
    assert a_order.volume > b_order.volume
    assert abs(a_order.volume - 2 * b_order.volume) <= 0.02
    # A risk ≤ 1% of $50k = $500
    a_loss = a_order.volume * 0.003 * 100000
    assert a_loss <= 500 + 1e-3
    assert "A" in a_order.comment


def test_draft_order_volatile_symbol_tighter_ceiling(mock_broker: MockBroker) -> None:
    """XAUUSD A-grade uses 0.8% not 1.0% due to override."""
    order = decide.draft_order(
        symbol="XAUUSD", side=OrderSide.BUY,
        entry=2300.00, stop=2290.00, target=2320.00,
        broker=mock_broker, grade=ConvictionGrade.A,
    )
    # Risk ≤ 0.8% of $50k = $400. Distance=10, contract_size=100.
    loss = order.volume * 10.0 * 100
    assert loss <= 400 + 1e-3


def test_rubric_grade_a_requires_5_of_5() -> None:
    r = SetupRubric(True, True, True, True, True)
    assert r.score == 5
    assert r.grade == ConvictionGrade.A


def test_rubric_grade_b_when_less_than_5() -> None:
    r = SetupRubric(True, True, True, True, False)
    assert r.score == 4
    assert r.grade == ConvictionGrade.B


def test_rubric_without_playbook_match_is_b_regardless() -> None:
    """Even if other 4 items true, missing playbook match forces B (should skip)."""
    r = SetupRubric(False, True, True, True, True)
    assert r.grade == ConvictionGrade.B


def test_risk_pct_for_respects_grade_and_override() -> None:
    assert decide.risk_pct_for("EURUSD", ConvictionGrade.B) == 0.5
    assert decide.risk_pct_for("EURUSD", ConvictionGrade.A) == 1.0
    assert decide.risk_pct_for("XAUUSD", ConvictionGrade.B) == 0.4
    assert decide.risk_pct_for("XAUUSD", ConvictionGrade.A) == 0.8
    assert decide.risk_pct_for("NAS100", ConvictionGrade.A) == 0.8


def test_preflight_reports_state(mock_broker: MockBroker) -> None:
    report = decide.preflight(mock_broker, stage="demo", now=WED_10_UTC)
    assert report.stage == "demo"
    assert report.balance == 50000.0
    assert report.equity == 50000.0
    assert not report.firm_violation
    assert not report.hard_stop_hit
    # At Wed 10:00 UTC, FX sessions should be open
    assert "EURUSD" in report.tradeable_symbols

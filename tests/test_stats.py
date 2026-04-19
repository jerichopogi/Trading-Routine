"""Trade-log parsing and cohort statistics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import stats


def _order_event(ticket: int, symbol: str, side: str, comment: str,
                 price: float, sl: float, ts: datetime) -> dict:
    return {
        "event": "order",
        "ts": ts.isoformat(),
        "stage": "demo",
        "order": {
            "symbol": symbol, "side": side, "volume": 0.1,
            "sl": sl, "tp": price + 0.005,
            "comment": comment, "magic": 424242,
        },
        "result": {
            "ok": True, "ticket": ticket, "price": price,
            "message": "ok", "request": {},
        },
    }


def _close_event(ticket: int, symbol: str, side: str, comment: str,
                 price_open: float, price_close: float, sl: float,
                 opened_ts: datetime, closed_ts: datetime) -> dict:
    return {
        "event": "close",
        "ts": closed_ts.isoformat(),
        "stage": "demo",
        "ok": True,
        "reason": "midday",
        "position": {
            "ticket": ticket,
            "symbol": symbol,
            "side": side,
            "volume": 0.1,
            "price_open": price_open,
            "price_current": price_close,
            "sl": sl,
            "tp": price_open + 0.01,
            "profit": 0.0,
            "swap": 0.0,
            "time_open": opened_ts.isoformat(),
            "comment": comment,
            "magic": 424242,
        },
    }


def _seed_trade_log(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_load_trades_empty(isolated_memory: Path) -> None:
    assert stats.load_trades() == []


def test_load_trades_parses_setup_and_grade(isolated_memory: Path) -> None:
    opened = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    closed = datetime(2026, 4, 20, 11, 0, tzinfo=UTC)
    path = isolated_memory / "trade-log.jsonl"
    _seed_trade_log(path, [
        _order_event(100, "EURUSD", "buy", "london-breakout|A",
                     price=1.0750, sl=1.0720, ts=opened),
        _close_event(100, "EURUSD", "buy", "london-breakout|A",
                     price_open=1.0750, price_close=1.0810, sl=1.0720,
                     opened_ts=opened, closed_ts=closed),
    ])
    trades = stats.load_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t.setup == "london-breakout"
    assert t.grade == "A"
    assert t.is_closed
    # Price moved +60 pips, stop was -30 pips → +2R
    assert t.r_multiple is not None
    assert abs(t.r_multiple - 2.0) < 0.01


def test_winning_cohort_math(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = []
    # 3 wins at +2R, 2 losses at -1R
    for i, (exit_price, sl_price) in enumerate([
        (1.0810, 1.0720),  # +2R
        (1.0810, 1.0720),  # +2R
        (1.0810, 1.0720),  # +2R
        (1.0720, 1.0720),  # at stop → 0R (edge of loser)
        (1.0680, 1.0720),  # below stop → beyond -1R due to slippage simulation
    ]):
        ticket = 100 + i
        opened = base + timedelta(minutes=i * 10)
        closed = opened + timedelta(minutes=30)
        events.extend([
            _order_event(ticket, "EURUSD", "buy", "london-breakout|A",
                         price=1.0750, sl=sl_price, ts=opened),
            _close_event(ticket, "EURUSD", "buy", "london-breakout|A",
                         price_open=1.0750, price_close=exit_price, sl=sl_price,
                         opened_ts=opened, closed_ts=closed),
        ])
    _seed_trade_log(path, events)
    trades = stats.load_trades()
    cohort = stats.cohort_of(trades)
    assert cohort.n == 5
    assert cohort.wins == 3
    assert cohort.avg_r < 2.0
    assert cohort.total_r > 0


def test_by_setup_separates_cohorts(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = [
        _order_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0720, base),
        _close_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0810, 1.0720, base, base + timedelta(hours=1)),
        _order_event(2, "GBPUSD", "buy", "ny-momentum|B",
                     1.2700, 1.2680, base + timedelta(hours=2)),
        _close_event(2, "GBPUSD", "buy", "ny-momentum|B",
                     1.2700, 1.2690, 1.2680, base + timedelta(hours=2),
                     base + timedelta(hours=3)),
    ]
    _seed_trade_log(path, events)
    trades = stats.load_trades()
    by_s = stats.by_setup(trades)
    assert "london-breakout" in by_s
    assert "ny-momentum" in by_s
    assert by_s["london-breakout"].avg_r > 0  # winner
    assert by_s["ny-momentum"].avg_r < 0      # loser


def test_auto_disable_flags_triggered(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = []
    # 6 losers on "bad-setup" — below threshold → flag
    for i in range(6):
        ticket = 200 + i
        opened = base + timedelta(hours=i)
        closed = opened + timedelta(minutes=45)
        events.extend([
            _order_event(ticket, "EURUSD", "buy", "bad-setup|B",
                         1.0750, 1.0720, opened),
            _close_event(ticket, "EURUSD", "buy", "bad-setup|B",
                         1.0750, 1.0690, 1.0720, opened, closed),
        ])
    _seed_trade_log(path, events)
    trades = stats.load_trades()
    flags = stats.auto_disable_flags(trades, min_sample=5, max_avg_r=-0.2)
    assert any(f.setup == "bad-setup" for f in flags)


def test_open_trade_still_tracked(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    _seed_trade_log(path, [
        _order_event(777, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0720, base),
        # No close event — still open
    ])
    trades = stats.load_trades()
    assert len(trades) == 1
    assert not trades[0].is_closed
    assert trades[0].r_multiple is None


def test_streak_tracks_latest(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = []
    # Pattern: W, W, L, L, L → streak should be 3 losses
    outcomes = [(1.0810, True), (1.0810, True),
                (1.0690, False), (1.0690, False), (1.0690, False)]
    for i, (exit_price, _) in enumerate(outcomes):
        ticket = 300 + i
        opened = base + timedelta(hours=i)
        closed = opened + timedelta(minutes=30)
        events.extend([
            _order_event(ticket, "EURUSD", "buy", "london-breakout|A",
                         1.0750, 1.0720, opened),
            _close_event(ticket, "EURUSD", "buy", "london-breakout|A",
                         1.0750, exit_price, 1.0720, opened, closed),
        ])
    _seed_trade_log(path, events)
    trades = stats.load_trades()
    streak = stats.current_streak(trades)
    assert streak.direction == "losses"
    assert streak.length == 3


def test_similar_trades_filter(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = [
        _order_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0720, base),
        _close_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0810, 1.0720, base, base + timedelta(hours=1)),
        _order_event(2, "GBPUSD", "buy", "london-breakout|B",
                     1.2700, 1.2670, base + timedelta(hours=2)),
        _close_event(2, "GBPUSD", "buy", "london-breakout|B",
                     1.2700, 1.2680, 1.2670, base + timedelta(hours=2),
                     base + timedelta(hours=3)),
    ]
    _seed_trade_log(path, events)
    trades = stats.load_trades()
    eur_only = stats.similar_trades(trades, symbol="EURUSD")
    assert len(eur_only) == 1
    assert eur_only[0].symbol == "EURUSD"

    a_only = stats.similar_trades(trades, grade="A")
    assert len(a_only) == 1
    assert a_only[0].grade == "A"


def test_performance_markdown_no_trades(isolated_memory: Path) -> None:
    md = stats.performance_markdown()
    assert "Performance snapshot" in md
    assert "No closed trades" in md


def test_performance_markdown_renders_cohorts(isolated_memory: Path) -> None:
    path = isolated_memory / "trade-log.jsonl"
    base = datetime.now(UTC) - timedelta(days=1)
    _seed_trade_log(path, [
        _order_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0720, base),
        _close_event(1, "EURUSD", "buy", "london-breakout|A",
                     1.0750, 1.0810, 1.0720, base, base + timedelta(hours=1)),
    ])
    md = stats.performance_markdown()
    assert "london-breakout" in md
    assert "EURUSD" in md


def test_write_performance_report(isolated_memory: Path) -> None:
    path = stats.write_performance_report()
    assert path.exists()
    assert path.name == "performance.md"
    assert "Performance snapshot" in path.read_text()

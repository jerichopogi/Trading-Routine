"""Memory file I/O."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts import journal
from scripts.broker import OrderRequest, OrderResult, OrderSide, Position


def test_log_order_writes_jsonl(isolated_memory: Path) -> None:
    order = OrderRequest(
        symbol="EURUSD", side=OrderSide.BUY, volume=0.1, sl=1.07, tp=1.08,
    )
    result = OrderResult(ok=True, ticket=99, price=1.075, message="ok", request=order)
    journal.log_order(order=order, result=result, stage="demo")

    log_file = isolated_memory / "trade-log.jsonl"
    assert log_file.exists()
    line = log_file.read_text().strip()
    record = json.loads(line)
    assert record["event"] == "order"
    assert record["stage"] == "demo"
    assert record["order"]["symbol"] == "EURUSD"


def test_log_close(isolated_memory: Path) -> None:
    p = Position(
        ticket=42, symbol="EURUSD", side=OrderSide.BUY, volume=0.1,
        price_open=1.075, price_current=1.078, sl=1.07, tp=1.08,
        profit=30.0, swap=0.0, time_open=datetime.now(UTC), comment="test",
    )
    journal.log_close(position=p, ok=True, reason="midday", stage="demo")

    lines = (isolated_memory / "trade-log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "close"
    assert record["ok"] is True
    assert record["position"]["ticket"] == 42


def test_append_daily_note_creates_file(isolated_memory: Path) -> None:
    path = journal.append_daily_note("setup", "bought EURUSD at 1.075")
    assert path.exists()
    content = path.read_text()
    assert "setup" in content
    assert "bought EURUSD" in content


def test_append_daily_note_appends(isolated_memory: Path) -> None:
    journal.append_daily_note("idea 1", "first")
    journal.append_daily_note("idea 2", "second")
    today = datetime.now(UTC).date().isoformat()
    path = isolated_memory / "daily-journal" / f"{today}.md"
    content = path.read_text()
    assert "idea 1" in content
    assert "idea 2" in content
    # Only one top-level heading
    assert content.count(f"# {today}") == 1


def test_read_strategy_and_playbook_missing(isolated_memory: Path) -> None:
    # Memory dir is isolated — strategy/playbook from repo aren't here
    assert journal.read_strategy() == ""
    assert journal.read_playbook() == ""


def test_read_strategy_present(isolated_memory: Path) -> None:
    (isolated_memory / "strategy.md").write_text("# strategy\n")
    assert "strategy" in journal.read_strategy()

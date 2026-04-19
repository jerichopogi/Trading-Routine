"""Performance statistics from trade-log.jsonl.

Turns the append-only trade log into decision-ready cohorts:
- by setup (parsed from order comment prefix)
- by symbol
- by conviction grade (A / B)
- by hour-of-day UTC
- by weekday

The weekly-review routine regenerates `memory/performance.md` from this module;
pre-session and session-open routines read that file before placing trades.

Trade reconstruction: we pair each `close` event with the most recent `order`
event for the same ticket. Open positions (no close yet) are tracked separately.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import clock
from .account import memory_dir


@dataclass(frozen=True)
class TradeRecord:
    ticket: int
    symbol: str
    side: str                   # "buy" | "sell"
    setup: str                  # first segment of comment, or "unknown"
    grade: str                  # "A" | "B" | "?"
    entry: float
    exit: float | None          # None if still open
    sl: float | None
    tp: float | None
    opened_at: datetime
    closed_at: datetime | None
    profit: float | None
    stage: str

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None

    @property
    def r_multiple(self) -> float | None:
        """Realized return in units of initial risk. None if no SL or still open."""
        if self.sl is None or self.exit is None:
            return None
        risk = abs(self.entry - self.sl)
        if risk == 0:
            return None
        move = (self.exit - self.entry) if self.side == "buy" else (self.entry - self.exit)
        return move / risk

    @property
    def is_winner(self) -> bool | None:
        r = self.r_multiple
        return None if r is None else r > 0


@dataclass(frozen=True)
class CohortStats:
    n: int
    wins: int
    losses: int
    win_rate: float         # 0..1
    avg_r: float
    total_r: float
    best_r: float
    worst_r: float

    @property
    def expectancy(self) -> float:
        return self.avg_r  # alias for clarity


@dataclass(frozen=True)
class Streak:
    direction: str          # "wins" | "losses" | "none"
    length: int


@dataclass(frozen=True)
class DisableFlag:
    setup: str
    reason: str
    n: int
    avg_r: float


DEFAULT_TRADE_LOG = "trade-log.jsonl"


def trade_log_path() -> Path:
    return memory_dir() / DEFAULT_TRADE_LOG


def _parse_setup(comment: str) -> str:
    if not comment:
        return "unknown"
    first = comment.split("|", 1)[0].strip()
    return first or "unknown"


def _parse_grade(comment: str) -> str:
    if not comment or "|" not in comment:
        return "?"
    tail = comment.split("|", 1)[1].strip()
    return tail.upper()[:1] if tail else "?"


def _read_events(path: Path | None = None) -> list[dict]:
    p = path or trade_log_path()
    if not p.exists():
        return []
    events: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def load_trades(
    start: datetime | None = None,
    end: datetime | None = None,
    path: Path | None = None,
) -> list[TradeRecord]:
    """Reconstruct trades from the log. Pairs `close` events with preceding `order` events."""
    events = _read_events(path)
    orders: dict[int, dict] = {}    # ticket -> last order event
    trades: list[TradeRecord] = []

    for ev in events:
        if ev.get("event") == "order":
            result = ev.get("result", {})
            if not result.get("ok"):
                continue
            ticket = result.get("ticket")
            if ticket is None or ticket < 0:   # dev-stage simulated (-1) ignored
                continue
            orders[ticket] = ev
        elif ev.get("event") == "close":
            pos = ev.get("position", {})
            ticket = pos.get("ticket")
            if ticket is None:
                continue
            opened = orders.pop(ticket, None)
            side = pos.get("side") or (opened and opened.get("order", {}).get("side")) or ""
            comment = pos.get("comment") or (opened and opened.get("order", {}).get("comment")) or ""
            trades.append(TradeRecord(
                ticket=ticket,
                symbol=pos.get("symbol", "?"),
                side=side,
                setup=_parse_setup(comment),
                grade=_parse_grade(comment),
                entry=float(pos.get("price_open", 0.0)),
                exit=float(pos.get("price_current", 0.0)),
                sl=pos.get("sl"),
                tp=pos.get("tp"),
                opened_at=_parse_dt(pos.get("time_open")),
                closed_at=_parse_dt(ev.get("ts")),
                profit=pos.get("profit"),
                stage=ev.get("stage", "?"),
            ))

    # Open positions — still have an order event but no close
    for ticket, opened in orders.items():
        order = opened.get("order", {})
        result = opened.get("result", {})
        trades.append(TradeRecord(
            ticket=ticket,
            symbol=order.get("symbol", "?"),
            side=order.get("side", ""),
            setup=_parse_setup(order.get("comment", "")),
            grade=_parse_grade(order.get("comment", "")),
            entry=float(result.get("price", 0.0)),
            exit=None,
            sl=order.get("sl"),
            tp=order.get("tp"),
            opened_at=_parse_dt(opened.get("ts")),
            closed_at=None,
            profit=None,
            stage=opened.get("stage", "?"),
        ))

    if start or end:
        trades = [
            t for t in trades
            if (start is None or t.opened_at >= start)
            and (end is None or t.opened_at <= end)
        ]
    return trades


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=UTC)


def _empty_cohort() -> CohortStats:
    return CohortStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)


def cohort_of(trades: list[TradeRecord]) -> CohortStats:
    closed = [t for t in trades if t.is_closed and t.r_multiple is not None]
    if not closed:
        return _empty_cohort()
    rs = [t.r_multiple for t in closed]  # type: ignore[misc]
    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r <= 0)
    return CohortStats(
        n=len(closed),
        wins=wins,
        losses=losses,
        win_rate=wins / len(closed),
        avg_r=sum(rs) / len(closed),
        total_r=sum(rs),
        best_r=max(rs),
        worst_r=min(rs),
    )


def by_setup(trades: list[TradeRecord]) -> dict[str, CohortStats]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[t.setup].append(t)
    return {k: cohort_of(v) for k, v in groups.items()}


def by_symbol(trades: list[TradeRecord]) -> dict[str, CohortStats]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[t.symbol].append(t)
    return {k: cohort_of(v) for k, v in groups.items()}


def by_grade(trades: list[TradeRecord]) -> dict[str, CohortStats]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[t.grade].append(t)
    return {k: cohort_of(v) for k, v in groups.items()}


def by_weekday(trades: list[TradeRecord]) -> dict[int, CohortStats]:
    groups: dict[int, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        groups[t.opened_at.weekday()].append(t)
    return {k: cohort_of(v) for k, v in groups.items()}


def current_streak(trades: list[TradeRecord]) -> Streak:
    closed = [t for t in trades if t.is_closed and t.is_winner is not None]
    closed.sort(key=lambda t: t.closed_at or datetime.fromtimestamp(0, tz=UTC))
    if not closed:
        return Streak("none", 0)
    last = closed[-1].is_winner
    direction = "wins" if last else "losses"
    length = 0
    for t in reversed(closed):
        if t.is_winner == last:
            length += 1
        else:
            break
    return Streak(direction, length)


def similar_trades(
    trades: list[TradeRecord],
    *,
    symbol: str | None = None,
    setup: str | None = None,
    grade: str | None = None,
    limit: int = 20,
) -> list[TradeRecord]:
    filtered = trades
    if symbol:
        filtered = [t for t in filtered if t.symbol == symbol]
    if setup:
        filtered = [t for t in filtered if t.setup == setup]
    if grade:
        filtered = [t for t in filtered if t.grade == grade]
    filtered.sort(key=lambda t: t.opened_at, reverse=True)
    return filtered[:limit]


def auto_disable_flags(
    trades: list[TradeRecord],
    *,
    min_sample: int = 5,
    max_avg_r: float = -0.2,
) -> list[DisableFlag]:
    """Setups with at least `min_sample` closed trades and avg R below threshold."""
    setup_cohorts = by_setup(trades)
    flags: list[DisableFlag] = []
    for setup, stats in setup_cohorts.items():
        if setup == "unknown":
            continue
        if stats.n >= min_sample and stats.avg_r <= max_avg_r:
            flags.append(DisableFlag(
                setup=setup,
                reason=f"{stats.n} trades, avg R={stats.avg_r:+.2f}, win_rate={stats.win_rate:.0%}",
                n=stats.n,
                avg_r=stats.avg_r,
            ))
    return flags


# ----- reporting -----

def performance_markdown(
    *,
    window_days: int = 30,
    all_trades_path: Path | None = None,
) -> str:
    """Human + LLM readable performance snapshot. Writes to memory/performance.md."""
    end = datetime.now(UTC)
    start = end - timedelta(days=window_days)
    trades = load_trades(start=start, end=end, path=all_trades_path)
    closed = [t for t in trades if t.is_closed]
    overall = cohort_of(closed)
    by_s = by_setup(closed)
    by_sym = by_symbol(closed)
    by_g = by_grade(closed)
    streak = current_streak(closed)
    flags = auto_disable_flags(closed)

    lines: list[str] = []
    ts = clock.format_display(end, "%Y-%m-%d %H:%M %Z")
    lines.append("# Performance snapshot")
    lines.append("")
    lines.append(f"_Generated: {ts} ({end.isoformat(timespec='seconds')})_")
    lines.append(f"_Window: last {window_days} days._")
    lines.append("")
    lines.append("## Overall")
    lines.append(f"- Trades closed: **{overall.n}**")
    lines.append(f"- Win rate: **{overall.win_rate:.0%}**")
    lines.append(f"- Avg R: **{overall.avg_r:+.2f}**")
    lines.append(f"- Total R: **{overall.total_r:+.2f}**")
    lines.append(f"- Best / worst: {overall.best_r:+.2f}R / {overall.worst_r:+.2f}R")
    lines.append(f"- Current streak: **{streak.length} {streak.direction}**")
    lines.append("")

    lines.append("## By conviction grade")
    lines.append("")
    lines.append("| Grade | N | Win% | Avg R | Total R |")
    lines.append("|-------|---|------|-------|---------|")
    for g in ("A", "B", "?"):
        s = by_g.get(g, _empty_cohort())
        if s.n > 0:
            lines.append(
                f"| {g} | {s.n} | {s.win_rate:.0%} | {s.avg_r:+.2f} | {s.total_r:+.2f} |"
            )
    lines.append("")

    lines.append("## By setup")
    lines.append("")
    lines.append("| Setup | N | Win% | Avg R | Total R | Status |")
    lines.append("|-------|---|------|-------|---------|--------|")
    disabled_setups = {f.setup for f in flags}
    for setup, s in sorted(by_s.items(), key=lambda kv: -kv[1].total_r):
        status = "⚠️ auto-disabled" if setup in disabled_setups else "active"
        lines.append(
            f"| {setup} | {s.n} | {s.win_rate:.0%} | {s.avg_r:+.2f} | "
            f"{s.total_r:+.2f} | {status} |"
        )
    lines.append("")

    lines.append("## By symbol")
    lines.append("")
    lines.append("| Symbol | N | Win% | Avg R | Total R |")
    lines.append("|--------|---|------|-------|---------|")
    for sym, s in sorted(by_sym.items(), key=lambda kv: -kv[1].total_r):
        lines.append(
            f"| {sym} | {s.n} | {s.win_rate:.0%} | {s.avg_r:+.2f} | {s.total_r:+.2f} |"
        )
    lines.append("")

    if flags:
        lines.append("## Auto-disable suggestions")
        lines.append("")
        lines.append(
            "These setups crossed the loss threshold. The weekly-review routine "
            "should propose disabling them in `playbook.md`; humans confirm."
        )
        lines.append("")
        for f in flags:
            lines.append(f"- **{f.setup}** — {f.reason}")
        lines.append("")

    if not closed:
        lines.append("_No closed trades in window yet — still gathering data._")

    return "\n".join(lines) + "\n"


def write_performance_report() -> Path:
    path = memory_dir() / "performance.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(performance_markdown(), encoding="utf-8")
    return path

"""Decision-layer helpers exposed to the LLM routines.

These are thin, deterministic helpers. The *decisions* happen inside the
routine prompt (Claude's judgment); this module just packages state.

Conviction-based sizing
-----------------------
`draft_order(grade=...)` sizes the trade based on setup quality:

- Grade A ("very good" — 5/5 on rubric): up to `per_trade_risk_pct_max` (1% default)
- Grade B ("okay" — meets playbook criteria): `per_trade_risk_pct` (0.5% default)

Rubric (see playbook.md for details) — scored by the LLM in the routine:
  1. Matches a specific playbook setup
  2. Higher-timeframe trend aligned with the trade direction
  3. No red-folder news within 1 hour of entry
  4. R:R at entry ≥ 2.0
  5. Price at a meaningful level (prior HH/LL, key MA, untouched supply/demand)

5/5 → A.  4 or fewer → B.  (No grading below B — if it can't hit 4/5, skip it.)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from . import sessions as session_clock
from .account import compute_rule_status, read_equity_curve
from .broker import Broker, OrderKind, OrderRequest, OrderSide
from .config import fundednext, instruments


class ConvictionGrade(StrEnum):
    A = "A"   # very good — max risk
    B = "B"   # standard — default risk


@dataclass(frozen=True)
class SetupRubric:
    """Checklist the LLM fills in to grade a setup. Must all be True for A-grade."""
    matches_playbook: bool
    htf_trend_aligned: bool
    clear_of_news: bool
    rr_ratio_ok: bool       # R:R ≥ 2.0 at intended entry
    at_meaningful_level: bool

    @property
    def score(self) -> int:
        return sum([
            self.matches_playbook,
            self.htf_trend_aligned,
            self.clear_of_news,
            self.rr_ratio_ok,
            self.at_meaningful_level,
        ])

    @property
    def grade(self) -> ConvictionGrade:
        if not self.matches_playbook:
            # Hard requirement — no playbook match, no trade (and certainly not A).
            return ConvictionGrade.B
        return ConvictionGrade.A if self.score == 5 else ConvictionGrade.B


@dataclass(frozen=True)
class PreflightReport:
    stage: str
    server: str
    balance: float
    equity: float
    open_positions: int
    daily_dd_pct: float
    max_dd_pct: float
    firm_violation: bool
    hard_stop_hit: bool
    reasons: list[str]
    tradeable_symbols: list[str]

    def summary(self) -> str:
        from . import clock
        now_utc = datetime.now(UTC)
        lines = [
            f"[{clock.format_display(now_utc, '%Y-%m-%d %H:%M %Z')} / {now_utc.strftime('%H:%M UTC')}]",
            f"stage={self.stage} server={self.server}",
            f"balance=${self.balance:,.2f} equity=${self.equity:,.2f}",
            f"daily_dd={self.daily_dd_pct:.2f}%  max_dd={self.max_dd_pct:.2f}%",
            f"open_positions={self.open_positions}  tradeable={len(self.tradeable_symbols)}",
        ]
        if self.reasons:
            lines.append("reasons: " + "; ".join(self.reasons))
        return "\n".join(lines)


def preflight(broker: Broker, stage: str, now: datetime | None = None) -> PreflightReport:
    """Snapshot state, check rules, return a decision-ready report."""
    now = now or datetime.now(UTC)
    info = broker.account_info()
    curve = read_equity_curve()
    rs = compute_rule_status(info, curve=curve, now=now)

    tradeable: list[str] = []
    whitelist = set(fundednext()["instruments"]["whitelist"])
    for sym in instruments():
        if sym not in whitelist:
            continue
        try:
            if session_clock.is_open(sym, now=now):
                tradeable.append(sym)
        except KeyError:
            continue

    return PreflightReport(
        stage=stage,
        server=info.server,
        balance=info.balance,
        equity=info.equity,
        open_positions=len(broker.positions()),
        daily_dd_pct=rs.daily_dd_pct,
        max_dd_pct=rs.max_dd_pct,
        firm_violation=rs.any_firm_violation,
        hard_stop_hit=rs.any_violation,
        reasons=list(rs.reasons),
        tradeable_symbols=tradeable,
    )


def size_by_risk(
    *,
    symbol: str,
    entry: float,
    stop: float,
    balance: float,
    risk_pct: float,
    contract_size: float,
) -> float:
    """Return volume in lots so loss at stop is at most `risk_pct` of balance.

    Rounds DOWN to 2 decimals (MT5 standard lot step). Rounding down ensures
    we never exceed the configured ceiling due to lot-size quantization.
    """
    distance = abs(entry - stop)
    if distance == 0 or contract_size == 0:
        return 0.0
    dollars_to_risk = balance * (risk_pct / 100.0)
    lots = dollars_to_risk / (distance * contract_size)
    # Floor to 2 decimals — stays at or under the ceiling.
    return math.floor(lots * 100) / 100


def risk_pct_for(symbol: str, grade: ConvictionGrade) -> float:
    """The per-trade risk % this symbol uses at the given conviction grade."""
    cfg = fundednext()
    overrides = cfg["instruments"].get("overrides", {}).get(symbol, {})
    if grade == ConvictionGrade.A:
        return float(
            overrides.get("per_trade_risk_pct_max", cfg["risk"]["per_trade_risk_pct_max"])
        )
    return float(
        overrides.get("per_trade_risk_pct", cfg["risk"]["per_trade_risk_pct"])
    )


def classify_entry(
    *, side: OrderSide, entry: float, bid: float, ask: float, tolerance: float | None = None,
) -> OrderKind:
    """Decide MARKET / LIMIT / STOP by comparing entry to current bid/ask.

    Tolerance defaults to spread × 2 so entries that are "at market" after a
    tick-level wiggle still route as MARKET. Beyond that, direction decides:

      BUY  + entry < bid  → LIMIT (pullback to buy zone)
      BUY  + entry > ask  → STOP  (buy on breakout)
      SELL + entry > ask  → LIMIT (rally to sell zone)
      SELL + entry < bid  → STOP  (sell on breakdown)
    """
    spread = max(ask - bid, 0.0)
    tol = tolerance if tolerance is not None else max(spread * 2.0, 1e-9)
    reference = ask if side == OrderSide.BUY else bid
    if abs(entry - reference) <= tol:
        return OrderKind.MARKET
    if side == OrderSide.BUY:
        return OrderKind.LIMIT if entry < bid else OrderKind.STOP
    return OrderKind.LIMIT if entry > ask else OrderKind.STOP


def draft_order(
    *,
    symbol: str,
    side: OrderSide,
    entry: float,
    stop: float,
    target: float,
    broker: Broker,
    grade: ConvictionGrade = ConvictionGrade.B,
    comment: str = "",
    kind: OrderKind | None = None,
) -> OrderRequest:
    """Compose an OrderRequest with volume sized to per-trade risk config.

    `grade` picks which risk % to use:
      - A ("very good" setup — 5/5 rubric): up to per_trade_risk_pct_max
      - B ("okay" setup, default): per_trade_risk_pct

    `kind` defaults to auto-classify via `classify_entry()`: MARKET if entry ≈
    current price, LIMIT if entry is on the favorable side (pullback), STOP if
    on the unfavorable side (breakout). Pass an explicit kind to override.

    Sizing reference depends on kind:
      - MARKET: uses the broker's current ask/bid (actual fill price), so the
        guardrail's risk calculation against that same fill price matches the
        sized volume. Using intended entry here would under-estimate distance
        by the spread and cause A-grade trades to be rejected at the ceiling.
      - LIMIT / STOP: uses the requested `entry` because the broker guarantees
        the fill at that price. The `_PendingRiskBroker` in trade.py mirrors
        this by feeding the guardrail entry-as-fill-price for pending orders.

    The guardrail layer still enforces the MAX as a hard ceiling regardless.
    """
    risk_pct = risk_pct_for(symbol, grade)
    info = broker.account_info()
    sym = broker.symbol_info(symbol)
    if kind is None:
        kind = classify_entry(side=side, entry=entry, bid=sym.bid, ask=sym.ask)
    if kind == OrderKind.MARKET:
        sizing_ref = sym.ask if side == OrderSide.BUY else sym.bid
    else:
        sizing_ref = entry
    volume = size_by_risk(
        symbol=symbol, entry=sizing_ref, stop=stop,
        balance=info.balance, risk_pct=risk_pct,
        contract_size=sym.contract_size,
    )
    # Tag the comment with the grade so we can analyze performance by conviction later.
    graded_comment = f"{comment}|{grade.value}"[:31] if comment else f"grade:{grade.value}"
    return OrderRequest(
        symbol=symbol, side=side, volume=volume,
        sl=stop, tp=target, comment=graded_comment,
        kind=kind,
        entry=entry if kind != OrderKind.MARKET else None,
    )

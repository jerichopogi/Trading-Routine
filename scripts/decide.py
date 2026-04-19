"""Decision-layer helpers exposed to the LLM routines.

These are thin, deterministic helpers. The *decisions* happen inside the
routine prompt (Claude's judgment); this module just packages state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from . import sessions as session_clock
from .account import compute_rule_status, read_equity_curve
from .broker import Broker, OrderRequest, OrderSide
from .config import fundednext, instruments


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
        lines = [
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
    """Return volume in lots so loss at stop equals `risk_pct` of balance."""
    distance = abs(entry - stop)
    if distance == 0 or contract_size == 0:
        return 0.0
    dollars_to_risk = balance * (risk_pct / 100.0)
    lots = dollars_to_risk / (distance * contract_size)
    return round(lots, 2)


def draft_order(
    *,
    symbol: str,
    side: OrderSide,
    entry: float,
    stop: float,
    target: float,
    broker: Broker,
    comment: str = "",
) -> OrderRequest:
    """Compose an OrderRequest with volume sized to per-trade risk config."""
    cfg = fundednext()
    risk_pct = float(cfg["risk"]["per_trade_risk_pct"])
    overrides = cfg["instruments"].get("overrides", {})
    if symbol in overrides and "per_trade_risk_pct" in overrides[symbol]:
        risk_pct = float(overrides[symbol]["per_trade_risk_pct"])
    info = broker.account_info()
    sym = broker.symbol_info(symbol)
    volume = size_by_risk(
        symbol=symbol, entry=entry, stop=stop,
        balance=info.balance, risk_pct=risk_pct,
        contract_size=sym.contract_size,
    )
    return OrderRequest(
        symbol=symbol, side=side, volume=volume,
        sl=stop, tp=target, comment=comment[:31],
    )

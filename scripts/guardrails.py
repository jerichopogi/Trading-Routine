"""FundedNext rule enforcement.

The LLM is NEVER between you and a blown account. Every order placement
passes through `check_or_reject()` which evaluates:
  - account-level drawdown state (account.is_in_violation)
  - per-trade risk
  - position caps
  - instrument whitelist
  - session windows
  - weekend flatten window

If any check fails, the order is rejected with a structured reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import sessions
from .account import compute_rule_status, read_equity_curve
from .broker import Broker, OrderRequest, SymbolInfo
from .config import fundednext


@dataclass(frozen=True)
class GuardrailVerdict:
    ok: bool
    reasons: list[str]
    metadata: dict[str, object]

    @property
    def blocked(self) -> bool:
        return not self.ok


def _risk_pct_for(symbol: str, cfg_risk: dict, overrides: dict) -> float:
    override = overrides.get(symbol, {})
    return float(override.get("per_trade_risk_pct", cfg_risk["per_trade_risk_pct"]))


def _estimate_risk_dollars(
    order: OrderRequest, sym: SymbolInfo
) -> float | None:
    """USD loss if the stop is hit. None if no SL set."""
    if order.sl is None:
        return None
    entry = sym.ask if order.side.value == "buy" else sym.bid
    distance = abs(entry - order.sl)
    if distance == 0:
        return None
    return distance * sym.contract_size * order.volume


def _new_positions_today(broker: Broker, now: datetime) -> int:
    today = now.date()
    return sum(1 for p in broker.positions() if p.time_open.date() == today)


def check_or_reject(
    order: OrderRequest,
    broker: Broker,
    now: datetime | None = None,
) -> GuardrailVerdict:
    """Evaluate every FundedNext + internal rule against this order."""
    now = now or datetime.now(tz=None)
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)

    cfg = fundednext()
    reasons: list[str] = []
    meta: dict[str, object] = {}

    # --- instrument whitelist ---
    whitelist = set(cfg["instruments"]["whitelist"])
    if order.symbol not in whitelist:
        reasons.append(f"{order.symbol} not in FundedNext whitelist")

    # --- account-level DD ---
    info = broker.account_info()
    curve = read_equity_curve()
    rule_status = compute_rule_status(info, curve=curve, now=now)
    meta["daily_dd_pct"] = rule_status.daily_dd_pct
    meta["max_dd_pct"] = rule_status.max_dd_pct
    if rule_status.any_violation or rule_status.any_firm_violation:
        reasons.extend(rule_status.reasons)

    # --- session windows ---
    try:
        if not sessions.is_open(order.symbol, now=now):
            reasons.append(f"{order.symbol} session closed")
    except KeyError:
        reasons.append(f"{order.symbol} has no session config")

    avoid, why = sessions.should_avoid_new_trades(now=now)
    if avoid:
        reasons.append(why)

    # --- position caps ---
    open_positions = broker.positions()
    max_concurrent = int(cfg["risk"]["max_concurrent_positions"])
    if len(open_positions) >= max_concurrent:
        reasons.append(
            f"{len(open_positions)} open ≥ cap {max_concurrent}"
        )
    max_new_per_day = int(cfg["risk"]["max_new_positions_per_day"])
    if _new_positions_today(broker, now) >= max_new_per_day:
        reasons.append(f"already opened {max_new_per_day} positions today")

    # --- per-trade risk ---
    try:
        sym = broker.symbol_info(order.symbol)
    except KeyError as e:
        reasons.append(f"symbol_info: {e}")
        return GuardrailVerdict(ok=False, reasons=reasons, metadata=meta)

    risk_pct_limit = _risk_pct_for(
        order.symbol, cfg["risk"], cfg["instruments"].get("overrides", {})
    )
    meta["risk_pct_limit"] = risk_pct_limit
    if order.sl is None:
        reasons.append("order has no stop loss")
    else:
        risk_usd = _estimate_risk_dollars(order, sym)
        if risk_usd is not None:
            risk_pct = 100.0 * risk_usd / max(info.balance, 1e-9)
            meta["risk_pct"] = round(risk_pct, 3)
            meta["risk_usd"] = round(risk_usd, 2)
            if risk_pct > risk_pct_limit + 1e-6:
                reasons.append(
                    f"trade risk {risk_pct:.2f}% > limit {risk_pct_limit}% for {order.symbol}"
                )

    # --- volume sanity ---
    if order.volume <= 0:
        reasons.append(f"invalid volume {order.volume}")

    return GuardrailVerdict(ok=len(reasons) == 0, reasons=reasons, metadata=meta)

"""Trade execution — the only code path that should reach `broker.place_order()`.

Every call goes through `guardrails.check_or_reject()` first; rejections are
logged and Discord-notified, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from . import journal, notify
from .broker import Broker, OrderRequest, OrderResult, OrderSide
from .guardrails import GuardrailVerdict, check_or_reject


@dataclass(frozen=True)
class TradeOutcome:
    placed: bool
    rejected: bool
    result: OrderResult | None
    verdict: GuardrailVerdict
    order: OrderRequest


def place(
    order: OrderRequest,
    broker: Broker,
    *,
    stage: str,
    notify_on_reject: bool = True,
) -> TradeOutcome:
    """Guardrail check, then place. Stage-gated (no real orders in dev)."""
    verdict = check_or_reject(order, broker=broker, now=datetime.now(UTC))

    if verdict.blocked:
        journal.log_rejection(order=order, verdict=verdict, stage=stage)
        if notify_on_reject:
            notify.warn(
                title=f"Trade rejected: {order.symbol} {order.side.value}",
                body="\n".join(f"- {r}" for r in verdict.reasons),
            )
        return TradeOutcome(placed=False, rejected=True, result=None, verdict=verdict, order=order)

    # Stage gate: dev = never place real orders. Only mock broker runs in dev.
    if stage == "dev":
        journal.log_order(
            order=order, result=OrderResult(
                ok=True, ticket=-1, price=None, message="dev stage — simulated", request=order,
            ), stage=stage,
        )

    result = broker.place_order(order)
    journal.log_order(order=order, result=result, stage=stage)

    if result.ok:
        notify.info(
            title=f"Trade placed: {order.symbol} {order.side.value} {order.volume}",
            body=(
                f"entry={result.price} sl={order.sl} tp={order.tp}\n"
                f"ticket={result.ticket} comment={order.comment}"
            ),
        )
    else:
        notify.warn(
            title=f"Broker rejected order: {order.symbol}",
            body=result.message,
        )

    return TradeOutcome(
        placed=result.ok, rejected=False, result=result, verdict=verdict, order=order,
    )


def flatten_all(broker: Broker, *, reason: str, stage: str) -> list[int]:
    """Close every open position. Returns list of closed tickets."""
    closed: list[int] = []
    for p in broker.positions():
        ok = broker.close_position(p.ticket)
        journal.log_close(position=p, ok=ok, reason=reason, stage=stage)
        if ok:
            closed.append(p.ticket)
    notify.info(
        title=f"Flatten all ({len(closed)} closed)",
        body=f"Reason: {reason}\nTickets: {closed}",
    )
    return closed


def tighten_stops_to_breakeven(broker: Broker, *, min_r: float = 1.0) -> int:
    """Move SL to entry for any position >= min_r in profit. Returns count moved."""
    moved = 0
    for p in broker.positions():
        if p.r_multiple is None or p.r_multiple < min_r:
            continue
        be = p.price_open
        if p.sl is not None and (
            (p.side == OrderSide.BUY and p.sl >= be) or (p.side == OrderSide.SELL and p.sl <= be)
        ):
            continue
        if broker.modify_position(p.ticket, sl=be):
            moved += 1
            journal.log_modify(ticket=p.ticket, new_sl=be, reason=f"breakeven at {min_r}R")
    return moved

"""Trade execution — the only code path that should reach `broker.place_order()`.

Every call goes through `guardrails.check_or_reject()` first; rejections are
logged and Discord-notified, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from . import journal, notify
from .broker import Broker, OrderKind, OrderRequest, OrderResult, OrderSide
from .guardrails import GuardrailVerdict, check_or_reject


@dataclass(frozen=True)
class TradeOutcome:
    placed: bool
    rejected: bool
    result: OrderResult | None
    verdict: GuardrailVerdict
    order: OrderRequest


class _PendingRiskBroker:
    """Broker wrapper that reports the pending entry as bid/ask for the
    guardrail's per-trade risk calculation, so a LIMIT/STOP order's risk is
    measured against its guaranteed fill price — not the current market price
    which it will never actually fill at. Every other broker call is passed
    through unchanged.
    """

    def __init__(self, broker: Broker, order: OrderRequest) -> None:
        self._broker = broker
        self._order = order

    def __getattr__(self, name):
        return getattr(self._broker, name)

    def symbol_info(self, symbol: str):
        si = self._broker.symbol_info(symbol)
        if symbol != self._order.symbol or self._order.entry is None:
            return si
        if self._order.side == OrderSide.BUY:
            return replace(si, ask=self._order.entry)
        return replace(si, bid=self._order.entry)


def place(
    order: OrderRequest,
    broker: Broker,
    *,
    stage: str,
    notify_on_reject: bool = True,
) -> TradeOutcome:
    """Guardrail check, then place. Stage-gated (no real orders in dev)."""
    # Pending orders: wrap the broker so guardrail risk math uses entry, not current price.
    check_broker = _PendingRiskBroker(broker, order) if order.kind != OrderKind.MARKET else broker
    verdict = check_or_reject(order, broker=check_broker, now=datetime.now(UTC))

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
        simulated = OrderResult(
            ok=True, ticket=-1, price=None, message="dev stage — simulated", request=order,
        )
        journal.log_order(order=order, result=simulated, stage=stage)
        return TradeOutcome(
            placed=False, rejected=False, result=simulated, verdict=verdict, order=order,
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


def flatten_all(
    broker: Broker, *, reason: str, stage: str, cancel_pending: bool = True,
) -> list[int]:
    """Close every open position. Returns list of closed tickets.

    When `cancel_pending=True` (default), also cancels every un-triggered
    pending order so stale intents don't carry into the next session. Set
    to False to flatten positions only (rarely needed).
    """
    closed: list[int] = []
    for p in broker.positions():
        ok = broker.close_position(p.ticket)
        journal.log_close(position=p, ok=ok, reason=reason, stage=stage)
        if ok:
            closed.append(p.ticket)

    cancelled: list[int] = []
    if cancel_pending:
        for po in broker.pending_orders():
            if broker.cancel_pending_order(po.ticket):
                cancelled.append(po.ticket)
                journal.log_cancel_pending(ticket=po.ticket, symbol=po.symbol, reason=reason)

    notify.info(
        title=f"Flatten all ({len(closed)} closed, {len(cancelled)} pending cancelled)",
        body=f"Reason: {reason}\nClosed: {closed}\nCancelled pending: {cancelled}",
    )
    return closed


def cancel_all_pending(broker: Broker, *, reason: str) -> list[int]:
    """Cancel every pending (unfilled) order. Returns the list of cancelled tickets.

    Call this at session-close to prevent stale intents from carrying overnight.
    """
    cancelled: list[int] = []
    for po in broker.pending_orders():
        if broker.cancel_pending_order(po.ticket):
            cancelled.append(po.ticket)
            journal.log_cancel_pending(ticket=po.ticket, symbol=po.symbol, reason=reason)
    if cancelled:
        notify.info(
            title=f"Cancelled {len(cancelled)} pending order(s)",
            body=f"Reason: {reason}\nTickets: {cancelled}",
        )
    return cancelled


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

"""Active trade management — breakeven, partial take-profits, trailing SL.

Runs mechanically (no LLM judgment) on a fast cadence (~15 min). Applies
a fixed rule table to every open position:

  r >= 1R and SL not at entry      -> move SL to entry (breakeven)
  r >= 2R and no partial yet       -> close 50% of ORIGINAL volume + SL to +1R
  r >= 3R and partial <= 70%       -> close another 20% + SL to +2R
  r >  3R (above scaled-out point) -> trail SL so it stays 1R behind current
                                      price; never move SL backward

Each rule is idempotent — re-running at the same price is a no-op.

The "original volume" is inferred from the position's current volume plus
any partials already recorded in trade-log.jsonl. This lets the routine
remain stateless — restart-safe, re-run-safe.

Guardrails are NOT invoked here; closing size and modifying SL don't open
new exposure. The only broker operations used are `modify_position` and
`partial_close_position`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import journal, notify
from .broker import Broker, OrderSide, Position


# --- Tunable rule table -----------------------------------------------------
# (trigger_r, close_fraction_of_ORIGINAL_volume, sl_to_r)
PARTIAL_SCHEDULE: tuple[tuple[float, float, float], ...] = (
    (2.0, 0.50, 1.0),
    (3.0, 0.20, 2.0),
)
BREAKEVEN_MIN_R = 1.0
TRAIL_R_STEP = 1.0         # above the last partial, trail SL this many R behind price
TRAIL_ACTIVATE_R = 3.0     # trailing kicks in once r crosses this


@dataclass(frozen=True)
class ManageReport:
    positions_touched: int = 0
    breakevens_moved: int = 0
    partials_taken: int = 0
    trails_updated: int = 0


def _sl_risk_per_unit(position: Position) -> float | None:
    """Risk derived from the current SL — only accurate before breakeven."""
    if position.sl is None:
        return None
    d = abs(position.price_open - position.sl)
    return d if d > 0 else None


def _current_r(position: Position, risk: float) -> float:
    move = (
        position.price_current - position.price_open
        if position.side == OrderSide.BUY
        else position.price_open - position.price_current
    )
    return move / risk


def _sl_at_r(position: Position, r: float, risk: float) -> float:
    direction = 1.0 if position.side == OrderSide.BUY else -1.0
    return position.price_open + direction * r * risk


def _is_better_sl(position: Position, proposed: float) -> bool:
    """True iff `proposed` tightens the stop (never loosens)."""
    if position.sl is None:
        return True
    if position.side == OrderSide.BUY:
        return proposed > position.sl + 1e-9
    return proposed < position.sl - 1e-9


def _original_volume(position: Position, partial_log: dict[int, float]) -> float:
    """Current volume plus sum of closed-partial volumes from the log."""
    return position.volume + partial_log.get(position.ticket, 0.0)


def _load_trade_log_state(path: Path | None = None) -> tuple[dict[int, float], dict[int, float]]:
    """Return (partial_volume_by_ticket, original_sl_by_ticket) from trade-log.

    Reading the initial SL from the `order` event lets us compute R after
    breakeven has already moved SL to entry (which would otherwise zero the
    risk denominator).
    """
    from .account import memory_dir
    p = path or (memory_dir() / "trade-log.jsonl")
    if not p.exists():
        return {}, {}
    partials: dict[int, float] = {}
    original_sl: dict[int, float] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = ev.get("event")
        if event == "partial_close":
            t = ev.get("ticket")
            v = ev.get("volume_closed", 0.0)
            if t is not None:
                partials[t] = partials.get(t, 0.0) + float(v)
        elif event == "order":
            result = ev.get("result", {})
            if not result.get("ok"):
                continue
            ticket = result.get("ticket")
            if ticket is None or ticket < 0:
                continue
            sl = ev.get("order", {}).get("sl")
            if sl is not None:
                # Last order event for this ticket wins — if a ticket is reused
                # (possible in MockBroker / rare in MT5), the most recent order
                # is what opened the currently-live position.
                original_sl[ticket] = float(sl)
        elif event == "close":
            pos = ev.get("position", {})
            ticket = pos.get("ticket")
            # When a position closes, clear its partial accumulator so a
            # future re-use of the ticket starts fresh.
            if ticket is not None:
                partials.pop(ticket, None)
    return partials, original_sl


def manage_runners(broker: Broker) -> ManageReport:
    """Apply the management rule table to every open position. Idempotent."""
    partials_by_ticket, original_sl_by_ticket = _load_trade_log_state()

    be_moved = 0
    partials_taken = 0
    trails = 0
    touched = 0

    for pos in list(broker.positions()):
        # Pin the original risk once — SL may move below (breakeven) but R-math
        # must stay anchored to the entry->original-SL distance.
        original_sl = original_sl_by_ticket.get(pos.ticket)
        if original_sl is None:
            # Fallback: the position still has its original SL (no breakeven yet)
            original_sl = pos.sl
        if original_sl is None:
            continue  # no SL ever = can't R-math
        risk = abs(pos.price_open - original_sl)
        if risk <= 0:
            continue

        r = _current_r(pos, risk)
        if r < BREAKEVEN_MIN_R:
            continue

        touched += 1
        original_vol = _original_volume(pos, partials_by_ticket)
        realized_frac = partials_by_ticket.get(pos.ticket, 0.0) / max(original_vol, 1e-9)

        # --- Step 1: breakeven ------------------------------------------------
        be_target = pos.price_open
        if _is_better_sl(pos, be_target):
            if broker.modify_position(pos.ticket, sl=be_target):
                be_moved += 1
                journal.log_modify(
                    ticket=pos.ticket, new_sl=be_target, reason="breakeven",
                )
                pos = _refresh(broker, pos.ticket) or pos

        # --- Step 2 / 3: partial take-profits --------------------------------
        for trigger_r, _, sl_to_r in PARTIAL_SCHEDULE:
            if r < trigger_r:
                continue
            target_realized = sum(
                f for (tr, f, _) in PARTIAL_SCHEDULE if tr <= trigger_r
            )
            if realized_frac + 1e-9 >= target_realized:
                continue
            remaining_to_close = target_realized - realized_frac
            vol_to_close = round(remaining_to_close * original_vol, 2)
            if vol_to_close < 0.01:
                continue
            if not broker.partial_close_position(pos.ticket, vol_to_close):
                continue
            realized_frac += vol_to_close / max(original_vol, 1e-9)
            partials_by_ticket[pos.ticket] = partials_by_ticket.get(pos.ticket, 0.0) + vol_to_close
            partials_taken += 1
            pos = _refresh(broker, pos.ticket) or pos
            journal.log_partial_close(
                ticket=pos.ticket, symbol=pos.symbol,
                volume_closed=vol_to_close, realized_r=trigger_r,
                remaining_volume=pos.volume if pos else 0.0,
                reason=f"partial@{trigger_r}R",
            )
            if pos is None:
                break  # position fully closed
            sl_target = _sl_at_r(pos, sl_to_r, risk)
            if _is_better_sl(pos, sl_target):
                if broker.modify_position(pos.ticket, sl=sl_target):
                    journal.log_modify(
                        ticket=pos.ticket, new_sl=sl_target,
                        reason=f"post-partial@{trigger_r}R",
                    )
                    pos = _refresh(broker, pos.ticket) or pos

        if pos is None:
            continue

        # --- Step 4: trailing stop above TRAIL_ACTIVATE_R --------------------
        if r > TRAIL_ACTIVATE_R:
            trail_r = r - TRAIL_R_STEP
            sl_target = _sl_at_r(pos, trail_r, risk)
            if _is_better_sl(pos, sl_target):
                if broker.modify_position(pos.ticket, sl=sl_target):
                    trails += 1
                    journal.log_modify(
                        ticket=pos.ticket, new_sl=sl_target,
                        reason=f"trail@{r:.2f}R",
                    )

    report = ManageReport(
        positions_touched=touched,
        breakevens_moved=be_moved,
        partials_taken=partials_taken,
        trails_updated=trails,
    )

    if be_moved or partials_taken or trails:
        notify.info(
            title=f"Trade management: {partials_taken} partial, {be_moved} BE, {trails} trail",
            body=(
                f"Positions touched: {touched}\n"
                f"Breakevens moved: {be_moved}\n"
                f"Partials taken: {partials_taken}\n"
                f"Trails updated: {trails}"
            ),
        )
    return report


def _refresh(broker: Broker, ticket: int) -> Position | None:
    for p in broker.positions():
        if p.ticket == ticket:
            return p
    return None

"""In-memory broker for Mac dev and tests.

Simulates MT5 well enough to exercise the guardrail + trade layers end to end.
Not a market simulator — prices move only when the test updates them via the
`set_price` helper.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from .base import (
    AccountInfo,
    Bar,
    BrokerState,
    OrderKind,
    OrderRequest,
    OrderResult,
    OrderSide,
    PendingOrder,
    Position,
    SymbolInfo,
    Timeframe,
)


def _default_prices() -> dict[str, tuple[float, float]]:
    return {
        "EURUSD": (1.07500, 1.07502),
        "GBPUSD": (1.27000, 1.27003),
        "USDJPY": (155.000, 155.004),
        "USDCHF": (0.90000, 0.90003),
        "AUDUSD": (0.66000, 0.66003),
        "USDCAD": (1.36000, 1.36003),
        "NZDUSD": (0.60000, 0.60003),
        "XAUUSD": (2300.00, 2300.15),
        "XAGUSD": (27.000, 27.015),
        "US30": (38500.0, 38502.0),
        "NAS100": (17500.0, 17502.0),
        "SPX500": (5200.0, 5200.3),
        "GER40": (18500.0, 18502.0),
    }


_SYMBOL_DIGITS = {
    "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "USDCHF": 5, "AUDUSD": 5,
    "USDCAD": 5, "NZDUSD": 5, "XAUUSD": 2, "XAGUSD": 3,
    "US30": 2, "NAS100": 2, "SPX500": 2, "GER40": 2,
}
_SYMBOL_CONTRACT = {
    "EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 100000, "USDCHF": 100000,
    "AUDUSD": 100000, "USDCAD": 100000, "NZDUSD": 100000,
    "XAUUSD": 100, "XAGUSD": 5000,
    "US30": 1, "NAS100": 1, "SPX500": 1, "GER40": 1,
}


class MockBroker:
    """Broker implementation backed by in-memory state. No network calls."""

    def __init__(
        self,
        initial_balance: float | None = None,
        server: str = "Mock-Demo",
    ) -> None:
        bal = initial_balance if initial_balance is not None else float(
            os.environ.get("INITIAL_BALANCE", "50000")
        )
        self._initial_balance = bal
        self._balance = bal
        self._server = server
        self._prices: dict[str, tuple[float, float]] = _default_prices()
        self._state = BrokerState()
        self._connected = False
        self._clock: datetime | None = None

    # ----- lifecycle -----

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    # ----- test helpers (not on the Broker protocol) -----

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        self._prices[symbol] = (bid, ask)
        self._fill_triggered_pending(symbol, bid, ask)
        self._revalue_positions()

    def set_clock(self, now: datetime) -> None:
        self._clock = now

    def set_balance(self, balance: float) -> None:
        self._balance = balance

    @property
    def state(self) -> BrokerState:
        return self._state

    # ----- Broker protocol -----

    def account_info(self) -> AccountInfo:
        equity = self._balance + sum(p.profit for p in self._state.positions)
        margin = sum(p.volume * p.price_open * 0.01 for p in self._state.positions)
        return AccountInfo(
            login=0,
            currency="USD",
            balance=round(self._balance, 2),
            equity=round(equity, 2),
            margin=round(margin, 2),
            free_margin=round(equity - margin, 2),
            server=self._server,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol not in self._prices:
            raise KeyError(f"MockBroker has no price for {symbol!r}")
        bid, ask = self._prices[symbol]
        digits = _SYMBOL_DIGITS.get(symbol, 5)
        return SymbolInfo(
            symbol=symbol,
            digits=digits,
            point=10 ** -digits,
            contract_size=_SYMBOL_CONTRACT.get(symbol, 100000),
            bid=bid,
            ask=ask,
            trade_allowed=True,
        )

    def positions(self) -> list[Position]:
        return list(self._state.positions)

    def pending_orders(self) -> list[PendingOrder]:
        return list(self._state.pending_orders)

    def place_order(self, order: OrderRequest) -> OrderResult:
        if not self._connected:
            self.connect()
        if order.symbol not in self._prices:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"Unknown symbol {order.symbol}", request=order,
            )

        if order.kind != OrderKind.MARKET:
            if order.entry is None:
                return OrderResult(
                    ok=False, ticket=None, price=None,
                    message=f"{order.kind.value} order requires entry price", request=order,
                )
            ticket = self._state.next_ticket
            self._state.next_ticket += 1
            pending = PendingOrder(
                ticket=ticket,
                symbol=order.symbol,
                side=order.side,
                kind=order.kind,
                volume=order.volume,
                entry=order.entry,
                sl=order.sl,
                tp=order.tp,
                time_placed=self._clock or datetime.now(UTC),
                expires_at=order.expires_at,
                comment=order.comment,
                magic=order.magic,
            )
            self._state.pending_orders.append(pending)
            return OrderResult(
                ok=True, ticket=ticket, price=order.entry,
                message=f"pending {order.kind.value}", request=order,
            )

        bid, ask = self._prices[order.symbol]
        fill = ask if order.side == OrderSide.BUY else bid
        ticket = self._state.next_ticket
        self._state.next_ticket += 1
        position = Position(
            ticket=ticket,
            symbol=order.symbol,
            side=order.side,
            volume=order.volume,
            price_open=fill,
            price_current=fill,
            sl=order.sl,
            tp=order.tp,
            profit=0.0,
            swap=0.0,
            time_open=self._clock or datetime.now(UTC),
            comment=order.comment,
            magic=order.magic,
        )
        self._state.positions.append(position)
        return OrderResult(ok=True, ticket=ticket, price=fill, message="ok", request=order)

    def cancel_pending_order(self, ticket: int) -> bool:
        for i, po in enumerate(self._state.pending_orders):
            if po.ticket == ticket:
                self._state.pending_orders.pop(i)
                return True
        return False

    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> bool:
        for i, p in enumerate(self._state.positions):
            if p.ticket == ticket:
                self._state.positions[i] = Position(
                    ticket=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume,
                    price_open=p.price_open, price_current=p.price_current,
                    sl=sl if sl is not None else p.sl,
                    tp=tp if tp is not None else p.tp,
                    profit=p.profit, swap=p.swap, time_open=p.time_open,
                    comment=p.comment, magic=p.magic,
                )
                return True
        return False

    def close_position(self, ticket: int) -> bool:
        for i, p in enumerate(self._state.positions):
            if p.ticket == ticket:
                # Realize P/L against current price
                self._balance += p.profit
                self._state.closed_positions.append(p)
                self._state.positions.pop(i)
                return True
        return False

    def partial_close_position(self, ticket: int, volume_to_close: float) -> bool:
        if volume_to_close <= 0:
            return False
        for i, p in enumerate(self._state.positions):
            if p.ticket != ticket:
                continue
            # Cap close volume at the open position volume
            close_vol = min(volume_to_close, p.volume)
            if close_vol >= p.volume - 1e-9:
                # Full close
                self._balance += p.profit
                self._state.closed_positions.append(p)
                self._state.positions.pop(i)
                return True
            # Partial: realize the proportional P/L, reduce volume
            fraction = close_vol / p.volume
            realized = p.profit * fraction
            self._balance += realized
            remaining_vol = p.volume - close_vol
            new_profit = p.profit - realized
            self._state.positions[i] = Position(
                ticket=p.ticket, symbol=p.symbol, side=p.side, volume=remaining_vol,
                price_open=p.price_open, price_current=p.price_current,
                sl=p.sl, tp=p.tp, profit=round(new_profit, 2), swap=p.swap,
                time_open=p.time_open, comment=p.comment, magic=p.magic,
            )
            # Record the partial slice in closed history for stats
            partial = Position(
                ticket=p.ticket, symbol=p.symbol, side=p.side, volume=close_vol,
                price_open=p.price_open, price_current=p.price_current,
                sl=p.sl, tp=p.tp, profit=round(realized, 2), swap=0.0,
                time_open=p.time_open, comment=f"{p.comment}|partial", magic=p.magic,
            )
            self._state.closed_positions.append(partial)
            return True
        return False

    def rates(self, symbol: str, tf: Timeframe, count: int) -> list[Bar]:
        """Return flat bars at current price. Good enough for smoke tests."""
        if symbol not in self._prices:
            raise KeyError(f"MockBroker has no rates for {symbol!r}")
        bid, _ = self._prices[symbol]
        now = self._clock or datetime.now(UTC)
        step = {
            Timeframe.M1: timedelta(minutes=1),
            Timeframe.M5: timedelta(minutes=5),
            Timeframe.M15: timedelta(minutes=15),
            Timeframe.M30: timedelta(minutes=30),
            Timeframe.H1: timedelta(hours=1),
            Timeframe.H4: timedelta(hours=4),
            Timeframe.D1: timedelta(days=1),
        }[tf]
        return [
            Bar(time=now - step * i, open=bid, high=bid, low=bid, close=bid, tick_volume=0)
            for i in range(count, 0, -1)
        ]

    # ----- internal -----

    def _fill_triggered_pending(self, symbol: str, bid: float, ask: float) -> None:
        """Check all pending orders on this symbol; fill any whose trigger hit.

        Triggers (standard MT5 semantics):
          BUY  LIMIT: fills when ask <= entry  (pullback into buy level)
          SELL LIMIT: fills when bid >= entry  (rally into sell level)
          BUY  STOP:  fills when ask >= entry  (breakout above)
          SELL STOP:  fills when bid <= entry  (breakdown below)
        """
        now = self._clock or datetime.now(UTC)
        remaining: list[PendingOrder] = []
        for po in self._state.pending_orders:
            if po.symbol != symbol:
                remaining.append(po)
                continue
            triggered = False
            fill_price = po.entry
            if po.side == OrderSide.BUY and po.kind == OrderKind.LIMIT:
                triggered = ask <= po.entry
                fill_price = min(ask, po.entry)
            elif po.side == OrderSide.SELL and po.kind == OrderKind.LIMIT:
                triggered = bid >= po.entry
                fill_price = max(bid, po.entry)
            elif po.side == OrderSide.BUY and po.kind == OrderKind.STOP:
                triggered = ask >= po.entry
                fill_price = max(ask, po.entry)
            elif po.side == OrderSide.SELL and po.kind == OrderKind.STOP:
                triggered = bid <= po.entry
                fill_price = min(bid, po.entry)

            if not triggered:
                remaining.append(po)
                continue

            self._state.positions.append(Position(
                ticket=po.ticket,
                symbol=po.symbol,
                side=po.side,
                volume=po.volume,
                price_open=fill_price,
                price_current=fill_price,
                sl=po.sl,
                tp=po.tp,
                profit=0.0,
                swap=0.0,
                time_open=now,
                comment=po.comment,
                magic=po.magic,
            ))
        self._state.pending_orders = remaining

    def _revalue_positions(self) -> None:
        new_positions: list[Position] = []
        for p in self._state.positions:
            bid, ask = self._prices.get(p.symbol, (p.price_current, p.price_current))
            price_current = bid if p.side == OrderSide.BUY else ask
            contract = _SYMBOL_CONTRACT.get(p.symbol, 100000)
            direction = 1.0 if p.side == OrderSide.BUY else -1.0
            profit = (price_current - p.price_open) * direction * p.volume * contract
            new_positions.append(Position(
                ticket=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume,
                price_open=p.price_open, price_current=price_current,
                sl=p.sl, tp=p.tp, profit=round(profit, 2), swap=p.swap,
                time_open=p.time_open, comment=p.comment, magic=p.magic,
            ))
        self._state.positions = new_positions

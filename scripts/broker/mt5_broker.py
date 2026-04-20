"""Real MetaTrader 5 adapter.

Windows-only. The `MetaTrader5` pip package does not exist on macOS/Linux,
so the import is deferred into `connect()` — importing this module on Mac
does NOT raise; instantiating + connecting does.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from .base import (
    AccountInfo,
    Bar,
    OrderKind,
    OrderRequest,
    OrderResult,
    OrderSide,
    PendingOrder,
    Position,
    SymbolInfo,
    Timeframe,
)

_TF_MAP: dict[Timeframe, int] = {}  # populated inside connect() once mt5 is imported


class Mt5Broker:
    """Wraps the MetaTrader5 Python package."""

    def __init__(self) -> None:
        self._mt5: Any = None
        self._connected = False

    # ----- lifecycle -----

    def connect(self) -> None:
        if self._connected:
            return
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "MetaTrader5 package not available. This adapter only runs on "
                "Windows with the MT5 terminal installed and `pip install MetaTrader5`."
            ) from e

        self._mt5 = mt5
        _TF_MAP.update({
            Timeframe.M1:  mt5.TIMEFRAME_M1,
            Timeframe.M5:  mt5.TIMEFRAME_M5,
            Timeframe.M15: mt5.TIMEFRAME_M15,
            Timeframe.M30: mt5.TIMEFRAME_M30,
            Timeframe.H1:  mt5.TIMEFRAME_H1,
            Timeframe.H4:  mt5.TIMEFRAME_H4,
            Timeframe.D1:  mt5.TIMEFRAME_D1,
        })

        terminal_path = os.environ.get("MT5_TERMINAL_PATH") or None
        init_kwargs: dict[str, Any] = {}
        if terminal_path:
            init_kwargs["path"] = terminal_path

        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD")
        server = os.environ.get("MT5_SERVER")

        if login and password and server:
            init_kwargs.update(login=int(login), password=password, server=server)

        if not mt5.initialize(**init_kwargs):
            err = mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {err}")

        self._connected = True

    def disconnect(self) -> None:
        if self._mt5 is not None and self._connected:
            self._mt5.shutdown()
        self._connected = False

    # ----- Broker protocol -----

    def account_info(self) -> AccountInfo:
        self._ensure()
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info failed: {self._mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            server=info.server,
        )

    def symbol_info(self, symbol: str) -> SymbolInfo:
        self._ensure()
        si = self._mt5.symbol_info(symbol)
        if si is None:
            raise KeyError(f"MT5 unknown symbol {symbol!r}")
        if not si.visible:
            self._mt5.symbol_select(symbol, True)
            si = self._mt5.symbol_info(symbol)
        tick = self._mt5.symbol_info_tick(symbol)
        bid = tick.bid if tick else si.bid
        ask = tick.ask if tick else si.ask
        return SymbolInfo(
            symbol=symbol,
            digits=si.digits,
            point=si.point,
            contract_size=si.trade_contract_size,
            bid=bid,
            ask=ask,
            trade_allowed=bool(si.trade_mode != self._mt5.SYMBOL_TRADE_MODE_DISABLED),
        )

    def positions(self) -> list[Position]:
        self._ensure()
        raw = self._mt5.positions_get() or []
        out: list[Position] = []
        for r in raw:
            side = OrderSide.BUY if r.type == self._mt5.POSITION_TYPE_BUY else OrderSide.SELL
            out.append(Position(
                ticket=r.ticket,
                symbol=r.symbol,
                side=side,
                volume=r.volume,
                price_open=r.price_open,
                price_current=r.price_current,
                sl=r.sl or None,
                tp=r.tp or None,
                profit=r.profit,
                swap=r.swap,
                time_open=datetime.fromtimestamp(r.time, tz=UTC),
                comment=r.comment,
                magic=r.magic,
            ))
        return out

    def place_order(self, order: OrderRequest) -> OrderResult:
        self._ensure()
        allow_live = os.environ.get("ALLOW_LIVE_ORDERS", "0") == "1"
        if not allow_live:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message="ALLOW_LIVE_ORDERS!=1 (dry-run)", request=order,
            )

        if order.kind != OrderKind.MARKET:
            return self._place_pending(order)

        mt5 = self._mt5
        tick = mt5.symbol_info_tick(order.symbol)
        if tick is None:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"no tick for {order.symbol}", request=order,
            )
        price = tick.ask if order.side == OrderSide.BUY else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": mt5.ORDER_TYPE_BUY if order.side == OrderSide.BUY else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": order.sl or 0.0,
            "tp": order.tp or 0.0,
            "deviation": 20,
            "magic": order.magic,
            "comment": order.comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"order_send returned None: {mt5.last_error()}", request=order,
            )
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            ok=ok,
            ticket=result.order if ok else None,
            price=result.price if ok else None,
            message=f"retcode={result.retcode} {result.comment}",
            request=order,
        )

    def _place_pending(self, order: OrderRequest) -> OrderResult:
        mt5 = self._mt5
        if order.entry is None:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"{order.kind.value} order requires entry price", request=order,
            )
        mt5_type = self._pending_mt5_type(order.kind, order.side)
        if mt5_type is None:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"unsupported pending combo {order.kind.value}/{order.side.value}",
                request=order,
            )
        req: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": mt5_type,
            "price": order.entry,
            "sl": order.sl or 0.0,
            "tp": order.tp or 0.0,
            "deviation": 20,
            "magic": order.magic,
            "comment": order.comment[:31],
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        if order.expires_at is not None:
            req["type_time"] = mt5.ORDER_TIME_SPECIFIED
            req["expiration"] = int(order.expires_at.timestamp())
        else:
            req["type_time"] = mt5.ORDER_TIME_GTC
        result = mt5.order_send(req)
        if result is None:
            return OrderResult(
                ok=False, ticket=None, price=None,
                message=f"order_send returned None: {mt5.last_error()}", request=order,
            )
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        return OrderResult(
            ok=ok,
            ticket=result.order if ok else None,
            price=order.entry if ok else None,
            message=f"retcode={result.retcode} {result.comment}",
            request=order,
        )

    def _pending_mt5_type(self, kind: OrderKind, side: OrderSide) -> int | None:
        mt5 = self._mt5
        mapping = {
            (OrderKind.LIMIT, OrderSide.BUY):  mt5.ORDER_TYPE_BUY_LIMIT,
            (OrderKind.LIMIT, OrderSide.SELL): mt5.ORDER_TYPE_SELL_LIMIT,
            (OrderKind.STOP,  OrderSide.BUY):  mt5.ORDER_TYPE_BUY_STOP,
            (OrderKind.STOP,  OrderSide.SELL): mt5.ORDER_TYPE_SELL_STOP,
        }
        return mapping.get((kind, side))

    def pending_orders(self) -> list[PendingOrder]:
        self._ensure()
        mt5 = self._mt5
        raw = mt5.orders_get() or []
        out: list[PendingOrder] = []
        type_map = {
            mt5.ORDER_TYPE_BUY_LIMIT:  (OrderKind.LIMIT, OrderSide.BUY),
            mt5.ORDER_TYPE_SELL_LIMIT: (OrderKind.LIMIT, OrderSide.SELL),
            mt5.ORDER_TYPE_BUY_STOP:   (OrderKind.STOP,  OrderSide.BUY),
            mt5.ORDER_TYPE_SELL_STOP:  (OrderKind.STOP,  OrderSide.SELL),
        }
        for r in raw:
            classified = type_map.get(r.type)
            if classified is None:
                continue
            kind, side = classified
            vol = getattr(r, "volume_current", None) or getattr(r, "volume_initial", 0.0)
            out.append(PendingOrder(
                ticket=r.ticket,
                symbol=r.symbol,
                side=side,
                kind=kind,
                volume=vol,
                entry=r.price_open,
                sl=r.sl or None,
                tp=r.tp or None,
                time_placed=datetime.fromtimestamp(r.time_setup, tz=UTC),
                expires_at=(
                    datetime.fromtimestamp(r.time_expiration, tz=UTC)
                    if r.time_expiration else None
                ),
                comment=r.comment,
                magic=r.magic,
            ))
        return out

    def cancel_pending_order(self, ticket: int) -> bool:
        self._ensure()
        mt5 = self._mt5
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
        result = mt5.order_send(req)
        return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)

    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> bool:
        self._ensure()
        mt5 = self._mt5
        pos = next((p for p in self.positions() if p.ticket == ticket), None)
        if pos is None:
            return False
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": sl if sl is not None else (pos.sl or 0.0),
            "tp": tp if tp is not None else (pos.tp or 0.0),
        }
        result = mt5.order_send(request)
        return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)

    def close_position(self, ticket: int) -> bool:
        self._ensure()
        mt5 = self._mt5
        pos = next((p for p in self.positions() if p.ticket == ticket), None)
        if pos is None:
            return False
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return False
        close_side = mt5.ORDER_TYPE_SELL if pos.side == OrderSide.BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.side == OrderSide.BUY else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_side,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": pos.magic,
            "comment": "close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)

    def rates(self, symbol: str, tf: Timeframe, count: int) -> list[Bar]:
        self._ensure()
        raw = self._mt5.copy_rates_from_pos(symbol, _TF_MAP[tf], 0, count)
        if raw is None:
            return []
        out: list[Bar] = []
        for r in raw:
            out.append(Bar(
                time=datetime.fromtimestamp(int(r["time"]), tz=UTC),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
            ))
        return out

    # ----- internal -----

    def _ensure(self) -> None:
        if not self._connected:
            self.connect()

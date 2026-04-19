"""Broker protocol and shared data types.

Concrete implementations must not leak MT5-specific types past this boundary;
the rest of the codebase only deals with these dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


@dataclass(frozen=True)
class AccountInfo:
    login: int
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    server: str


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    digits: int
    point: float
    contract_size: float
    bid: float
    ask: float
    trade_allowed: bool


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    volume: float              # lots
    sl: float | None = None    # stop loss (price)
    tp: float | None = None    # take profit (price)
    comment: str = ""
    magic: int = 424242        # identifies trades placed by this agent


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    ticket: int | None
    price: float | None
    message: str
    request: OrderRequest


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    side: OrderSide
    volume: float
    price_open: float
    price_current: float
    sl: float | None
    tp: float | None
    profit: float
    swap: float
    time_open: datetime
    comment: str = ""
    magic: int = 0

    @property
    def r_multiple(self) -> float | None:
        """Realized P/L in units of initial risk. Requires sl to be set."""
        if self.sl is None:
            return None
        risk_per_unit = abs(self.price_open - self.sl)
        if risk_per_unit == 0:
            return None
        move = (
            self.price_current - self.price_open
            if self.side == OrderSide.BUY
            else self.price_open - self.price_current
        )
        return move / risk_per_unit


class Broker(Protocol):
    """Protocol implemented by MockBroker and Mt5Broker."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def account_info(self) -> AccountInfo: ...
    def symbol_info(self, symbol: str) -> SymbolInfo: ...
    def positions(self) -> list[Position]: ...
    def place_order(self, order: OrderRequest) -> OrderResult: ...
    def modify_position(
        self, ticket: int, sl: float | None = None, tp: float | None = None
    ) -> bool: ...
    def close_position(self, ticket: int) -> bool: ...
    def rates(self, symbol: str, tf: Timeframe, count: int) -> list[Bar]: ...


@dataclass
class BrokerState:
    """Shared scratch state used by the mock and available to tests."""

    positions: list[Position] = field(default_factory=list)
    closed_positions: list[Position] = field(default_factory=list)
    next_ticket: int = 10_000

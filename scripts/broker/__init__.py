"""Broker adapters. Use `get_broker()` instead of importing concrete classes."""

from __future__ import annotations

import os

from .base import (
    AccountInfo,
    Bar,
    Broker,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
    SymbolInfo,
    Timeframe,
)


def get_broker() -> Broker:
    """Return the broker configured via BROKER_MODE env var.

    mock: MockBroker (Mac dev, CI).
    mt5:  Mt5Broker (Windows only, requires MetaTrader5 package + running terminal).
    """
    mode = os.environ.get("BROKER_MODE", "mock").lower()
    if mode == "mock":
        from .mock_broker import MockBroker
        return MockBroker()
    if mode == "mt5":
        from .mt5_broker import Mt5Broker
        return Mt5Broker()
    raise ValueError(f"Unknown BROKER_MODE={mode!r}. Expected 'mock' or 'mt5'.")


__all__ = [
    "AccountInfo",
    "Bar",
    "Broker",
    "OrderRequest",
    "OrderResult",
    "OrderSide",
    "Position",
    "SymbolInfo",
    "Timeframe",
    "get_broker",
]

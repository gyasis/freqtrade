"""
execution/broker_backend.py
============================
IBrokerBackend — asset-class-agnostic execution interface for LATS.

Abstracts order placement, balance, and OHLCV data so that
GridTradingModule and other algorithm modules can run unchanged
against crypto, US equities, or forex backends.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from pandas import DataFrame

logger = logging.getLogger("algo_system.execution")


class IBrokerBackend(ABC):
    """
    Minimal execution interface all LATS backends must implement.

    Each backend maps these abstract operations to its broker/exchange API.
    LATS algorithm logic never calls a broker API directly — it always
    goes through this interface.
    """

    @property
    @abstractmethod
    def asset_class(self) -> str:
        """Return 'crypto', 'equity', or 'forex'."""
        ...

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """Unique identifier, e.g. 'freqtrade', 'alpaca', 'oanda'."""
        ...

    @abstractmethod
    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        n_candles: int = 100,
    ) -> DataFrame:
        """
        Return a DataFrame with columns: date(index), open, high, low, close, volume.
        Most recent candle is the last row.
        """
        ...

    @abstractmethod
    def get_balance(self) -> Dict[str, float]:
        """Return {currency: amount} for all non-zero balances."""
        ...

    @abstractmethod
    def get_total_capital(self) -> float:
        """Return total account value in quote currency."""
        ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,           # "buy" or "sell"
        amount: float,       # in base currency units
        order_type: str = "market",
        price: Optional[float] = None,
    ) -> dict:
        """
        Place an order. Returns a dict with at minimum:
          {"order_id": str, "symbol": str, "side": str,
           "amount": float, "status": str, "filled": float}
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel an open order."""
        ...

    @abstractmethod
    def get_open_positions(self) -> List[dict]:
        """
        Return list of open positions. Each dict has at minimum:
          {"symbol": str, "side": str, "amount": float,
           "entry_price": float, "unrealized_pnl": float}
        """
        ...

    @abstractmethod
    def get_symbols(self) -> List[str]:
        """Return all tradeable symbols available on this backend."""
        ...

    def is_paper_trading(self) -> bool:
        """Return True if this backend is in paper/sandbox mode. Override if needed."""
        return True


class MockBrokerBackend(IBrokerBackend):
    """
    In-memory mock backend for unit tests.
    No network calls. All data is synthetic or provided at construction.
    """

    def __init__(
        self,
        asset_class: str = "crypto",
        balance: Optional[Dict[str, float]] = None,
        ohlcv_df: Optional[DataFrame] = None,
    ) -> None:
        self._asset_class = asset_class
        self._balance: Dict[str, float] = balance or {"USDT": 10_000.0}
        self._ohlcv_df = ohlcv_df  # if None, generated on demand
        self._orders: List[dict] = []
        self._order_counter = 0
        self._positions: List[dict] = []

    @property
    def asset_class(self) -> str:
        return self._asset_class

    @property
    def backend_id(self) -> str:
        return f"mock_{self._asset_class}"

    def get_ohlcv(self, symbol: str, timeframe: str, n_candles: int = 100) -> DataFrame:
        if self._ohlcv_df is not None:
            return self._ohlcv_df.tail(n_candles).copy()
        # Generate synthetic OHLCV
        import numpy as np
        import pandas as pd
        from datetime import datetime, timezone
        rng = np.random.default_rng(42)
        closes = 50_000.0 * np.cumprod(1 + rng.normal(0, 0.01, n_candles))
        index = pd.date_range(end=datetime(2024, 1, 1, tzinfo=timezone.utc), periods=n_candles, freq="1h")
        return pd.DataFrame({
            "open": closes * (1 + rng.normal(0, 0.002, n_candles)),
            "high": closes * (1 + abs(rng.normal(0, 0.005, n_candles))),
            "low": closes * (1 - abs(rng.normal(0, 0.005, n_candles))),
            "close": closes,
            "volume": np.exp(rng.normal(10, 1, n_candles)),
        }, index=index)

    def get_balance(self) -> Dict[str, float]:
        return dict(self._balance)

    def get_total_capital(self) -> float:
        return sum(self._balance.values())

    def place_order(self, symbol: str, side: str, amount: float,
                    order_type: str = "market", price: Optional[float] = None) -> dict:
        self._order_counter += 1
        order = {
            "order_id": f"mock_{self._order_counter}",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "order_type": order_type,
            "price": price,
            "status": "filled",
            "filled": amount,
        }
        self._orders.append(order)
        logger.debug("MockBroker: placed %s %s %s %s", order_type, side, amount, symbol)
        return order

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self._orders = [o for o in self._orders if o["order_id"] != order_id]

    def get_open_positions(self) -> List[dict]:
        return list(self._positions)

    def get_symbols(self) -> List[str]:
        return ["BTC/USDT", "ETH/USDT", "AAPL", "EUR/USD"]

    def is_paper_trading(self) -> bool:
        return True

    # Test helpers
    def add_position(self, symbol: str, amount: float, entry_price: float) -> None:
        """Helper for tests to inject a position."""
        self._positions.append({
            "symbol": symbol,
            "side": "long",
            "amount": amount,
            "entry_price": entry_price,
            "unrealized_pnl": 0.0,
        })

    def get_order_history(self) -> List[dict]:
        return list(self._orders)

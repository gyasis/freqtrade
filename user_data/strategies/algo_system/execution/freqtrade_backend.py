"""
execution/freqtrade_backend.py
================================
FreqtradeBackend — IBrokerBackend adapter for freqtrade.

OrchestratorStrategy remains the actual IStrategy freqtrade loads.
This backend wraps it so other LATS components (screening, morning_run)
can interact with the freqtrade execution layer through the standard
IBrokerBackend interface.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pandas import DataFrame

from .broker_backend import IBrokerBackend

logger = logging.getLogger("algo_system.execution.freqtrade")


class FreqtradeBackend(IBrokerBackend):
    """
    Adapts freqtrade's DataProvider and Wallets to the IBrokerBackend interface.

    Instantiated by OrchestratorStrategy and passed to components that need
    backend-agnostic access to exchange data and account state.
    """

    def __init__(self, dp=None, wallets=None, config: dict = None) -> None:
        self._dp = dp          # freqtrade IDataProvider
        self._wallets = wallets  # freqtrade Wallets
        self._config = config or {}

    @property
    def asset_class(self) -> str:
        return "crypto"

    @property
    def backend_id(self) -> str:
        return "freqtrade"

    def get_ohlcv(self, symbol: str, timeframe: str, n_candles: int = 100) -> DataFrame:
        if self._dp is None:
            raise RuntimeError("FreqtradeBackend: DataProvider not set")
        df = self._dp.get_pair_dataframe(symbol, timeframe)
        return df.tail(n_candles).copy()

    def get_balance(self) -> Dict[str, float]:
        if self._wallets is None:
            return {}
        stake = self._config.get("stake_currency", "USDT")
        return {stake: self._wallets.get_free(stake)}

    def get_total_capital(self) -> float:
        if self._wallets is None:
            return 0.0
        return self._wallets.get_total_stake_amount()

    def place_order(self, symbol: str, side: str, amount: float,
                    order_type: str = "market", price: Optional[float] = None) -> dict:
        # Freqtrade manages order placement internally via IStrategy hooks.
        # This method is a passthrough stub — actual orders go via
        # adjust_trade_position / confirm_trade_entry in OrchestratorStrategy.
        logger.debug(
            "FreqtradeBackend.place_order: %s %s %s %s — handled by IStrategy hooks",
            order_type, side, amount, symbol,
        )
        return {"order_id": None, "symbol": symbol, "side": side,
                "amount": amount, "status": "delegated_to_strategy", "filled": 0.0}

    def cancel_order(self, order_id: str, symbol: str) -> None:
        logger.debug("FreqtradeBackend.cancel_order: delegated to freqtrade")

    def get_open_positions(self) -> List[dict]:
        # Open positions are managed by freqtrade's Trade model.
        # Return empty — callers that need trade data should use Trade directly.
        return []

    def get_symbols(self) -> List[str]:
        if self._dp is None:
            return []
        return self._dp.current_whitelist()

    def is_paper_trading(self) -> bool:
        try:
            from freqtrade.enums import RunMode
            return self._config.get("runmode") != RunMode.LIVE
        except ImportError:
            return True

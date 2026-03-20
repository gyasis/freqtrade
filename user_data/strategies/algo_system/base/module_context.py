"""
module_context.py — T005 LATS
Thin proxy facades and ModuleContext dataclass passed to every algo module.
No imports from other algo_system files to prevent circular dependencies.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

from pandas import DataFrame


class DataProviderProxy:
    """Facade over freqtrade's IDataProvider; exposes only the surface modules need."""

    def __init__(self, dp: Any) -> None:
        self._dp = dp

    def get_pair_dataframe(self, pair: str, timeframe: str) -> DataFrame:
        """Return OHLCV DataFrame for the requested pair/timeframe."""
        return self._dp.get_pair_dataframe(pair, timeframe)

    def current_whitelist(self) -> List[str]:
        """Return the current trading whitelist."""
        return self._dp.current_whitelist()

    def available_capital(self, exchange: Optional[str] = None) -> float:
        """Return available capital; falls back to 0.0 on older freqtrade versions."""
        try:
            return self._dp.available_capital(exchange)
        except AttributeError:
            return 0.0

    def ohlcv(self, pair: str, timeframe: str, copy: bool = True) -> DataFrame:
        """Return raw OHLCV data from the exchange cache."""
        return self._dp.ohlcv(pair, timeframe, copy=copy)


class WalletsProxy:
    """Facade over freqtrade's Wallets; exposes only balance-related methods."""

    def __init__(self, wallets: Any) -> None:
        self._wallets = wallets

    def get_free(self, currency: str) -> float:
        """Return free (unlocked) balance for the given currency."""
        return self._wallets.get_free(currency)

    def get_total_stake_amount(self) -> float:
        """Return total stake amount across all open trades and free balance."""
        return self._wallets.get_total_stake_amount()

    def get_trade_stake_amount(
        self, pair: str, _freqtrade_buy_tag: Optional[str] = None
    ) -> float:
        """Return stake amount for a prospective trade; 0.0 if unavailable."""
        try:
            return self._wallets.get_trade_stake_amount(pair)
        except Exception:  # noqa: BLE001 — guard against version differences
            return 0.0


@dataclass
class ModuleContext:
    """
    Immutable context bundle passed into every algo module on each candle.

    Attributes
    ----------
    pair:            Trading pair being evaluated (e.g. "BTC/USDT").
    run_mode:        freqtrade RunMode enum value (typed Any to avoid hard import).
    current_time:    UTC timestamp of the candle being processed.
    data_provider:   Thin proxy over freqtrade's IDataProvider.
    wallets:         Thin proxy over freqtrade's Wallets.
    shared_state:    SharedState instance (typed Any to avoid circular import).
    reasoning_hints: Optional RoutingDecision from the orchestrator pre-pass.
    logger:          Logger scoped to the calling module.
    module_id:       Unique identifier for the module producing/consuming this context.
    """

    pair: str
    run_mode: Any               # freqtrade RunMode enum — avoid hard import
    current_time: datetime
    data_provider: DataProviderProxy
    wallets: WalletsProxy
    shared_state: Any           # SharedState — forward ref as Any to avoid circular import
    reasoning_hints: Optional[Any]  # Optional[RoutingDecision]
    logger: logging.Logger
    module_id: str

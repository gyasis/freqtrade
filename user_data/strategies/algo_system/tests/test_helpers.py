"""
tests/test_helpers.py
Shared test helper functions and mock stubs for the LATS test suite.

Importable as a regular module (unlike conftest.py which is pytest-special).
conftest.py re-exports these as pytest fixtures.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pandas import DataFrame

# Make algo_system importable without freqtrade on PATH
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
if str(_STRAT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRAT_ROOT))

from algo_system.base.module_context import DataProviderProxy, ModuleContext, WalletsProxy
from algo_system.orchestrator.shared_state import SharedState


# ---------------------------------------------------------------------------
# OHLCV helpers
# ---------------------------------------------------------------------------

def make_ohlcv_df(
    n_candles: int = 100,
    start_price: float = 50_000.0,
    volatility: float = 0.01,
    seed: int = 42,
) -> DataFrame:
    """Return a realistic OHLCV DataFrame with *n_candles* random-walk rows."""
    rng = np.random.default_rng(seed)
    closes = [start_price]
    for _ in range(n_candles - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, volatility)))
    closes = np.array(closes)

    high = closes * (1 + abs(rng.normal(0, volatility / 2, n_candles)))
    low = closes * (1 - abs(rng.normal(0, volatility / 2, n_candles)))
    open_ = closes * (1 + rng.normal(0, volatility / 4, n_candles))
    volume = np.exp(rng.normal(10, 1, n_candles))

    end = datetime(2024, 1, 1, tzinfo=timezone.utc)
    index = pd.date_range(end=end, periods=n_candles, freq="1h")

    return DataFrame(
        {"date": index, "open": open_, "high": high,
         "low": low, "close": closes, "volume": volume}
    ).set_index("date")


def make_ranging_ohlcv_df(
    n_candles: int = 100,
    center_price: float = 50_000.0,
    band_pct: float = 0.03,
    seed: int = 99,
) -> DataFrame:
    """Return a narrow-band oscillating OHLCV DataFrame (low ADX expected)."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 4 * np.pi, n_candles)
    closes = center_price * (1 + band_pct * np.sin(t) + rng.normal(0, 0.001, n_candles))
    high = closes * (1 + abs(rng.normal(0, 0.001, n_candles)))
    low = closes * (1 - abs(rng.normal(0, 0.001, n_candles)))
    open_ = closes * (1 + rng.normal(0, 0.0005, n_candles))
    volume = np.exp(rng.normal(10, 0.5, n_candles))

    end = datetime(2024, 1, 1, tzinfo=timezone.utc)
    index = pd.date_range(end=end, periods=n_candles, freq="1h")

    return DataFrame(
        {"date": index, "open": open_, "high": high,
         "low": low, "close": closes, "volume": volume}
    ).set_index("date")


# ---------------------------------------------------------------------------
# Mock DataProvider / Wallets
# ---------------------------------------------------------------------------

class MockDataProvider:
    """Minimal DataProvider stub for unit tests."""

    def __init__(
        self,
        pair_dfs: Optional[Dict[str, DataFrame]] = None,
        whitelist: Optional[List[str]] = None,
    ) -> None:
        self._pair_dfs: Dict[str, DataFrame] = pair_dfs or {}
        self._whitelist: List[str] = whitelist or list(self._pair_dfs.keys())
        self._messages: List[dict] = []

    def get_pair_dataframe(self, pair: str, _timeframe: str) -> DataFrame:
        return self._pair_dfs.get(pair, make_ohlcv_df())

    def current_whitelist(self) -> List[str]:
        return list(self._whitelist)

    def available_capital(self, _exchange: Optional[str] = None) -> float:
        return 10_000.0

    def ohlcv(self, pair: str, _timeframe: str, copy: bool = True) -> DataFrame:
        df = self._pair_dfs.get(pair, make_ohlcv_df())
        return df.copy() if copy else df

    def send_msg(self, message: dict, _always_send: bool = False) -> None:
        self._messages.append(message)

    def runmode(self) -> Any:
        try:
            from freqtrade.enums import RunMode  # noqa: PLC0415
            return RunMode.BACKTEST
        except ImportError:
            return "backtest"


class MockWallets:
    """Minimal Wallets stub for unit tests."""

    def __init__(self, total_capital: float = 10_000.0, free: float = 10_000.0) -> None:
        self._total = total_capital
        self._free = free

    def get_free(self, _currency: str) -> float:
        return self._free

    def get_total_stake_amount(self) -> float:
        return self._total

    def get_trade_stake_amount(self, _pair: str, max_open_trades: int = 5) -> float:
        return self._total / max(max_open_trades, 1)


# ---------------------------------------------------------------------------
# ModuleContext factory
# ---------------------------------------------------------------------------

def make_module_context(
    pair: str = "BTC/USDT",
    pair_df: Optional[DataFrame] = None,
    whitelist: Optional[List[str]] = None,
    total_capital: float = 10_000.0,
    module_id: str = "test_module",
    shared_state: Optional[SharedState] = None,
) -> ModuleContext:
    """Build a ModuleContext wired to mock DataProvider and Wallets."""
    if pair_df is None:
        pair_df = make_ohlcv_df()
    if whitelist is None:
        whitelist = [pair]

    mock_dp = MockDataProvider(pair_dfs={pair: pair_df}, whitelist=whitelist)
    mock_wallets = MockWallets(total_capital=total_capital)

    dp_proxy = DataProviderProxy(mock_dp)
    wallets_proxy = WalletsProxy(mock_wallets)

    if shared_state is None:
        shared_state = SharedState(
            persistence_path=os.path.join(tempfile.gettempdir(), "lats_test_state.json")
        )

    try:
        from freqtrade.enums import RunMode  # noqa: PLC0415
        run_mode: Any = RunMode.BACKTEST
    except ImportError:
        run_mode = "backtest"

    return ModuleContext(
        pair=pair,
        run_mode=run_mode,
        current_time=datetime.now(timezone.utc),
        data_provider=dp_proxy,
        wallets=wallets_proxy,
        shared_state=shared_state,
        reasoning_hints=None,
        logger=logging.getLogger(f"test.{module_id}.{pair}"),
        module_id=module_id,
    )


# ---------------------------------------------------------------------------
# Mock Trade / Order stubs
# ---------------------------------------------------------------------------

class MockTrade:
    """Minimal Trade stub matching attributes accessed by GridTradingModule."""

    def __init__(
        self,
        pair: str = "BTC/USDT",
        open_rate: float = 50_000.0,
        enter_tag: str = "grid_trading_v1:initial_entry",
        stake_amount: float = 1000.0,
    ) -> None:
        self.pair = pair
        self.open_rate = open_rate
        self.enter_tag = enter_tag
        self.stake_amount = stake_amount
        self.amount = stake_amount / open_rate


class MockOrder:
    """Minimal Order stub with a fill price attribute."""

    def __init__(self, price: float, side: str = "buy", amount: float = 0.01) -> None:
        self.price = price
        self.average = price
        self.side = side
        self.amount = amount
        self.status = "closed"

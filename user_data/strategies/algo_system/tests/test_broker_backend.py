"""
tests/test_broker_backend.py
Tests for IBrokerBackend contract (via MockBrokerBackend) and FreqtradeBackend.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make algo_system importable without freqtrade on PATH
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
if str(_STRAT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRAT_ROOT))

import pytest

from algo_system.execution.broker_backend import IBrokerBackend, MockBrokerBackend


class TestIBrokerBackendContract:
    """MockBrokerBackend satisfies all IBrokerBackend abstract methods."""

    def test_is_subclass_of_ibroker(self):
        backend = MockBrokerBackend()
        assert isinstance(backend, IBrokerBackend)

    def test_asset_class_property(self):
        assert MockBrokerBackend(asset_class="crypto").asset_class == "crypto"
        assert MockBrokerBackend(asset_class="equity").asset_class == "equity"

    def test_get_ohlcv_returns_dataframe(self):
        backend = MockBrokerBackend()
        df = backend.get_ohlcv("BTC/USDT", "1h", 50)
        assert len(df) == 50
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns

    def test_get_balance_returns_dict(self):
        backend = MockBrokerBackend(balance={"USDT": 5000.0})
        bal = backend.get_balance()
        assert bal["USDT"] == 5000.0

    def test_get_total_capital(self):
        backend = MockBrokerBackend(balance={"USDT": 5000.0, "BTC": 1000.0})
        assert backend.get_total_capital() == 6000.0

    def test_place_order_returns_dict(self):
        backend = MockBrokerBackend()
        order = backend.place_order("BTC/USDT", "buy", 0.01)
        assert order["status"] == "filled"
        assert order["symbol"] == "BTC/USDT"
        assert order["side"] == "buy"

    def test_multiple_orders_have_unique_ids(self):
        backend = MockBrokerBackend()
        o1 = backend.place_order("BTC/USDT", "buy", 0.01)
        o2 = backend.place_order("ETH/USDT", "buy", 0.1)
        assert o1["order_id"] != o2["order_id"]

    def test_cancel_order_removes_from_history(self):
        backend = MockBrokerBackend()
        order = backend.place_order("BTC/USDT", "buy", 0.01)
        backend.cancel_order(order["order_id"], "BTC/USDT")
        history = backend.get_order_history()
        assert all(o["order_id"] != order["order_id"] for o in history)

    def test_get_open_positions_returns_list(self):
        backend = MockBrokerBackend()
        assert isinstance(backend.get_open_positions(), list)

    def test_add_position_helper(self):
        backend = MockBrokerBackend()
        backend.add_position("BTC/USDT", 0.1, 50000.0)
        positions = backend.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDT"

    def test_is_paper_trading_default_true(self):
        assert MockBrokerBackend().is_paper_trading() is True

    def test_get_symbols_returns_list(self):
        result = MockBrokerBackend().get_symbols()
        assert isinstance(result, list)
        assert len(result) > 0


class TestFreqtradeBackend:
    def test_asset_class_is_crypto(self):
        from algo_system.execution.freqtrade_backend import FreqtradeBackend
        assert FreqtradeBackend().asset_class == "crypto"

    def test_backend_id(self):
        from algo_system.execution.freqtrade_backend import FreqtradeBackend
        assert FreqtradeBackend().backend_id == "freqtrade"

    def test_place_order_returns_delegated_status(self):
        from algo_system.execution.freqtrade_backend import FreqtradeBackend
        backend = FreqtradeBackend()
        result = backend.place_order("BTC/USDT", "buy", 0.01)
        assert result["status"] == "delegated_to_strategy"

    def test_get_symbols_empty_without_dp(self):
        from algo_system.execution.freqtrade_backend import FreqtradeBackend
        assert FreqtradeBackend().get_symbols() == []

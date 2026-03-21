"""
tests/test_circuit_breaker.py
Unit tests for CircuitBreaker safety module (T028).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from pandas import DataFrame

_STRAT_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.safety.circuit_breaker import CircuitBreaker, CircuitBreakerStatus


def _make_cb(
    max_drawdown_pct: float = 0.15,
    price_move_pct: float = 0.08,
    price_move_candles: int = 3,
    cooling_candles: int = 5,
) -> CircuitBreaker:
    return CircuitBreaker({
        "max_drawdown_pct": max_drawdown_pct,
        "price_move_pct": price_move_pct,
        "price_move_candles": price_move_candles,
        "cooling_candles": cooling_candles,
    })


def _make_flat_df(n: int = 10, price: float = 50_000.0) -> DataFrame:
    """Flat-price OHLCV DataFrame — price move check should not trigger."""
    index = pd.date_range("2024-01-01", periods=n, freq="1h")
    return DataFrame({
        "open": price, "high": price * 1.001, "low": price * 0.999,
        "close": price, "volume": 1.0,
    }, index=index)


def _make_crash_df(n: int = 10, drop_pct: float = 0.15, price: float = 50_000.0) -> DataFrame:
    """DataFrame where last candle has dropped `drop_pct` from n-1 candles ago."""
    closes = [price] * n
    closes[-1] = price * (1 - drop_pct)
    index = pd.date_range("2024-01-01", periods=n, freq="1h")
    return DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": 1.0,
    }, index=index)


# ===========================================================================
# Initial state
# ===========================================================================

class TestCircuitBreakerInitialState:
    def test_starts_armed(self) -> None:
        cb = _make_cb()
        assert cb.state.status == CircuitBreakerStatus.ARMED

    def test_is_tripped_false_when_armed(self) -> None:
        cb = _make_cb()
        assert not cb.is_tripped()

    def test_config_values_applied(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.20, price_move_pct=0.05, cooling_candles=3)
        assert cb.state.max_drawdown_pct == pytest.approx(0.20)
        assert cb.state.price_move_pct == pytest.approx(0.05)
        assert cb.state.cooling_candles == 3


# ===========================================================================
# Drawdown threshold
# ===========================================================================

class TestCircuitBreakerDrawdown:
    def test_below_threshold_no_trip(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.15)
        df = _make_flat_df()
        assert cb.evaluate(df, portfolio_drawdown_pct=0.10) is False
        assert cb.state.status == CircuitBreakerStatus.ARMED

    def test_at_threshold_trips(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.15)
        df = _make_flat_df()
        result = cb.evaluate(df, portfolio_drawdown_pct=0.15)
        assert result is True
        assert cb.state.status == CircuitBreakerStatus.TRIPPED

    def test_above_threshold_trips(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.15)
        df = _make_flat_df()
        result = cb.evaluate(df, portfolio_drawdown_pct=0.25)
        assert result is True
        assert cb.is_tripped()

    def test_trip_reason_contains_drawdown_info(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.15)
        cb.evaluate(_make_flat_df(), portfolio_drawdown_pct=0.20)
        assert cb.state.trip_reason is not None
        assert "drawdown" in cb.state.trip_reason.lower() or "0.20" in cb.state.trip_reason

    def test_tripped_at_is_set(self) -> None:
        cb = _make_cb(max_drawdown_pct=0.15)
        cb.evaluate(_make_flat_df(), portfolio_drawdown_pct=0.20)
        assert cb.state.tripped_at is not None


# ===========================================================================
# Price move threshold
# ===========================================================================

class TestCircuitBreakerPriceMove:
    def test_no_trip_on_flat_price(self) -> None:
        cb = _make_cb(price_move_pct=0.08, price_move_candles=3)
        df = _make_flat_df(n=10)
        assert cb.evaluate(df, 0.0) is False

    def test_trips_on_large_price_drop(self) -> None:
        cb = _make_cb(price_move_pct=0.08, price_move_candles=3)
        df = _make_crash_df(n=10, drop_pct=0.12)  # 12% drop > 8% threshold
        result = cb.evaluate(df, 0.0)
        assert result is True
        assert cb.is_tripped()

    def test_no_trip_below_price_move_threshold(self) -> None:
        cb = _make_cb(price_move_pct=0.08, price_move_candles=3)
        df = _make_crash_df(n=10, drop_pct=0.04)  # 4% drop < 8% threshold
        assert cb.evaluate(df, 0.0) is False

    def test_insufficient_candles_skips_price_check(self) -> None:
        cb = _make_cb(price_move_pct=0.08, price_move_candles=5)
        df = _make_flat_df(n=3)  # fewer rows than price_move_candles
        # Should not trip from price move — not enough data
        assert cb.evaluate(df, 0.0) is False

    def test_price_surge_also_triggers(self) -> None:
        """A rapid price INCREASE should also trigger (abs move)."""
        cb = _make_cb(price_move_pct=0.08, price_move_candles=3)
        closes = [50_000.0] * 10
        closes[-1] = 50_000.0 * 1.15  # +15% surge
        index = pd.date_range("2024-01-01", periods=10, freq="1h")
        df = DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=index)
        assert cb.evaluate(df, 0.0) is True


# ===========================================================================
# State transitions: ARMED → TRIPPED → COOLING → ARMED
# ===========================================================================

class TestCircuitBreakerStateTransitions:
    def test_armed_to_tripped(self) -> None:
        cb = _make_cb()
        cb.trip("test reason")
        assert cb.state.status == CircuitBreakerStatus.TRIPPED
        assert cb.is_tripped()

    def test_tripped_to_cooling(self) -> None:
        cb = _make_cb(cooling_candles=5)
        cb.trip("reason")
        cb.begin_cooling()
        assert cb.state.status == CircuitBreakerStatus.COOLING
        assert not cb.is_tripped()

    def test_cooling_to_armed_after_countdown(self) -> None:
        cb = _make_cb(cooling_candles=3)
        cb.trip("reason")
        cb.begin_cooling()
        df = _make_flat_df()
        for _ in range(3):
            cb.evaluate(df, 0.0)
        assert cb.state.status == CircuitBreakerStatus.ARMED

    def test_evaluate_during_cooling_returns_false(self) -> None:
        cb = _make_cb(cooling_candles=5)
        cb.trip("reason")
        cb.begin_cooling()
        df = _make_flat_df()
        # First two candles during cooling should return False (trading allowed)
        assert cb.evaluate(df, 0.0) is False
        assert cb.evaluate(df, 0.0) is False

    def test_evaluate_while_tripped_returns_true(self) -> None:
        cb = _make_cb()
        cb.trip("reason")
        df = _make_flat_df()
        assert cb.evaluate(df, 0.0) is True

    def test_reset_clears_trip_metadata(self) -> None:
        cb = _make_cb()
        cb.trip("some reason")
        cb.reset()
        assert cb.state.status == CircuitBreakerStatus.ARMED
        assert cb.state.trip_reason is None
        assert cb.state.tripped_at is None

    def test_begin_cooling_noop_when_not_tripped(self) -> None:
        cb = _make_cb()
        cb.begin_cooling()  # should not raise or change state
        assert cb.state.status == CircuitBreakerStatus.ARMED

    def test_status_dict_has_expected_keys(self) -> None:
        cb = _make_cb()
        d = cb.get_status_dict()
        for key in ("status", "trip_reason", "cooling_counter", "max_drawdown_pct"):
            assert key in d

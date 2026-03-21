"""
tests/test_trend_following.py
==============================
Unit tests for TrendFollowingModule (LATS second trading algorithm).

Coverage targets
----------------
- Config loading and validation (defaults, bad values)
- populate_indicators: columns added, talib fallback path
- generate_entry_signal:
    - uptrend + healthy RSI → enter_long=True  (trade mode)
    - downtrend → enter_long=False
    - RSI out of band → no entry
    - insufficient ATR volatility → no entry
    - empty / NaN DataFrame → empty ModuleSignal
    - SMA200 gate (enabled / disabled)
- generate_exit_signal:
    - EMA fast < EMA slow → exit_long=True  (ema_reversal)
    - RSI overbought → exit_long=True  (rsi_overbought)
    - trailing stop breach → exit_long=True  (trail_stop)
    - healthy uptrend → exit_long=False
    - empty DataFrame → empty ModuleSignal
- Alert mode:
    - entry conditions met → enter_long=False
    - MetricsCollector.send_alert is called exactly once
    - same conditions in trade mode → enter_long=True
- adjust_position always returns None
- on_order_filled is a no-op (does not raise)
- get_module_state structure
- reset_module_state clears pair state
- on_bot_start populates whitelist pairs
- shutdown clears _pair_state
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

# ---------------------------------------------------------------------------
# Ensure algo_system is on sys.path regardless of test runner CWD
# ---------------------------------------------------------------------------
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.modules.trend_following.trend_following_module import (  # noqa: E402
    TrendFollowingModule,
    _atr_pandas,
    _ema_pandas,
    _rsi_pandas,
    _sma_pandas,
)
from algo_system.base.ialgo_module import ModuleSignal  # noqa: E402
from test_helpers import make_module_context, make_ohlcv_df  # noqa: E402


# ===========================================================================
# DataFrame construction helpers
# ===========================================================================

def _make_trending_up_df(
    n: int = 150,
    start: float = 100.0,
    step: float = 0.5,
    atr_fraction: float = 0.01,
) -> DataFrame:
    """
    Build a deterministically upward-trending OHLCV DataFrame.

    Closes rise linearly by ``step`` each candle, giving a clear EMA_fast >
    EMA_slow relationship after sufficient warm-up candles.  ATR is set to
    ``atr_fraction * close`` so the ATR filter passes by default.
    """
    closes = np.array([start + i * step for i in range(n)], dtype=float)
    atr_abs = closes * atr_fraction
    high = closes + atr_abs * 0.5
    low = closes - atr_abs * 0.5
    open_ = closes - step * 0.1
    volume = np.ones(n) * 1000.0

    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return DataFrame(
        {"open": open_, "high": high, "low": low, "close": closes, "volume": volume},
        index=idx,
    )


def _make_trending_down_df(
    n: int = 150,
    start: float = 200.0,
    step: float = 0.5,
) -> DataFrame:
    """Downward-trending OHLCV — EMA_fast < EMA_slow after warm-up."""
    closes = np.array([start - i * step for i in range(n)], dtype=float)
    high = closes + 0.5
    low = closes - 0.5
    open_ = closes + 0.1
    volume = np.ones(n) * 1000.0
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return DataFrame(
        {"open": open_, "high": high, "low": low, "close": closes, "volume": volume},
        index=idx,
    )


def _make_flat_df(n: int = 150, price: float = 100.0) -> DataFrame:
    """Flat OHLCV — EMAs converge; ATR will be near zero."""
    closes = np.full(n, price, dtype=float)
    high = closes + 0.01
    low = closes - 0.01
    open_ = closes.copy()
    volume = np.ones(n) * 1000.0
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return DataFrame(
        {"open": open_, "high": high, "low": low, "close": closes, "volume": volume},
        index=idx,
    )


def _inject_indicators(
    df: DataFrame,
    ema_fast: Optional[float] = None,
    ema_slow: Optional[float] = None,
    rsi: Optional[float] = None,
    sma200: Optional[float] = None,
    atr: Optional[float] = None,
) -> DataFrame:
    """
    Directly set indicator columns on the last row, bypassing talib/pandas.

    This lets tests control exact indicator values without relying on the
    indicator computation paths, giving precise control over which conditions
    fire.
    """
    df = df.copy()
    close = df["close"].iloc[-1]
    df["_tf_ema_fast"] = ema_fast if ema_fast is not None else close
    df["_tf_ema_slow"] = ema_slow if ema_slow is not None else close
    df["_tf_rsi"] = rsi if rsi is not None else 55.0
    df["_tf_sma200"] = sma200 if sma200 is not None else 0.0
    df["_tf_atr"] = atr if atr is not None else close * 0.01
    return df


def _make_module(
    mode: str = "trade",
    fast_period: int = 12,
    slow_period: int = 26,
    rsi_entry_min: float = 45.0,
    rsi_entry_max: float = 70.0,
    rsi_exit_overbought: float = 75.0,
    require_above_sma200: bool = False,
    atr_min_pct: float = 0.005,
    trail_stop_pct: float = 0.03,
    alert_channel: str = "lats_alerts",
) -> TrendFollowingModule:
    """
    Return an initialized TrendFollowingModule with the given config.

    Each call uses a fresh temporary file so SharedState never bleeds between
    tests — this is critical because SharedState is backed by a JSON file and
    a fixed path would cause earlier test configs to persist into later ones.
    """
    from algo_system.orchestrator.shared_state import SharedState
    import tempfile

    # Use a truly unique file per call to guarantee isolation
    tmp = tempfile.NamedTemporaryFile(
        suffix=".json", prefix="lats_test_tf_", delete=False
    )
    tmp.close()

    ss = SharedState(persistence_path=tmp.name)
    ss.set(
        "trend_following_v1",
        "config",
        {
            "mode": mode,
            "fast_period": fast_period,
            "slow_period": slow_period,
            "rsi_entry_min": rsi_entry_min,
            "rsi_entry_max": rsi_entry_max,
            "rsi_exit_overbought": rsi_exit_overbought,
            "require_above_sma200": require_above_sma200,
            "atr_min_pct": atr_min_pct,
            "trail_stop_pct": trail_stop_pct,
            "alert_channel": alert_channel,
        },
    )
    ctx = make_module_context(shared_state=ss)
    mod = TrendFollowingModule()
    mod.initialize(ctx)
    return mod


# ===========================================================================
# TestTrendFollowingModuleInit
# ===========================================================================


class TestTrendFollowingModuleInit:
    """Config loading, defaults, and validation errors."""

    def test_module_id_is_correct(self) -> None:
        assert TrendFollowingModule.module_id == "trend_following_v1"

    def test_version_is_semver(self) -> None:
        parts = TrendFollowingModule.version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_defaults_applied_when_config_empty(self) -> None:
        from algo_system.orchestrator.shared_state import SharedState
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".json", prefix="lats_tf_defaults_", delete=False)
        tmp.close()
        ss = SharedState(persistence_path=tmp.name)
        # Do NOT write any config key — all defaults should apply
        ctx = make_module_context(shared_state=ss)
        mod = TrendFollowingModule()
        mod.initialize(ctx)

        assert mod._config["mode"] == "trade"
        assert mod._config["fast_period"] == 12
        assert mod._config["slow_period"] == 26
        assert mod._config["rsi_period"] == 14
        assert mod._config["atr_period"] == 14
        assert mod._config["rsi_entry_min"] == pytest.approx(45.0)
        assert mod._config["rsi_entry_max"] == pytest.approx(70.0)
        assert mod._config["rsi_exit_overbought"] == pytest.approx(75.0)
        assert mod._config["require_above_sma200"] is True
        assert mod._config["atr_min_pct"] == pytest.approx(0.005)
        assert mod._config["trail_stop_pct"] == pytest.approx(0.03)
        assert mod._config["alert_channel"] == "lats_alerts"

    def _make_isolated_ss(self, config: dict) -> Any:
        """Create a SharedState with a unique temp file and pre-loaded config."""
        from algo_system.orchestrator.shared_state import SharedState
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".json", prefix="lats_tf_iso_", delete=False)
        tmp.close()
        ss = SharedState(persistence_path=tmp.name)
        ss.set("trend_following_v1", "config", config)
        return ss

    def test_invalid_mode_raises_value_error(self) -> None:
        ss = self._make_isolated_ss({"mode": "live_trade"})
        ctx = make_module_context(shared_state=ss)
        mod = TrendFollowingModule()
        with pytest.raises(ValueError, match="mode"):
            mod.initialize(ctx)

    def test_fast_geq_slow_raises_value_error(self) -> None:
        ss = self._make_isolated_ss({"fast_period": 26, "slow_period": 12})
        ctx = make_module_context(shared_state=ss)
        mod = TrendFollowingModule()
        with pytest.raises(ValueError, match="fast_period"):
            mod.initialize(ctx)

    def test_fast_equal_slow_raises_value_error(self) -> None:
        ss = self._make_isolated_ss({"fast_period": 14, "slow_period": 14})
        ctx = make_module_context(shared_state=ss)
        mod = TrendFollowingModule()
        with pytest.raises(ValueError, match="fast_period"):
            mod.initialize(ctx)

    def test_zero_fast_period_raises_value_error(self) -> None:
        ss = self._make_isolated_ss({"fast_period": 0, "slow_period": 26})
        ctx = make_module_context(shared_state=ss)
        mod = TrendFollowingModule()
        with pytest.raises(ValueError, match="fast_period"):
            mod.initialize(ctx)

    def test_alert_mode_accepted(self) -> None:
        mod = _make_module(mode="alert")
        assert mod._config["mode"] == "alert"

    def test_metrics_collector_is_set_after_init(self) -> None:
        mod = _make_module()
        assert mod._metrics is not None


# ===========================================================================
# TestPopulateIndicators
# ===========================================================================


class TestPopulateIndicators:
    """Verify indicator columns are attached and have correct shapes."""

    def _populated_df(self, require_sma200: bool = True) -> DataFrame:
        mod = _make_module(require_above_sma200=require_sma200)
        df = _make_trending_up_df(n=250)
        ctx = make_module_context()
        return mod.populate_indicators(df, {"pair": "BTC/USDT"}, ctx)

    def test_ema_fast_column_added(self) -> None:
        df = self._populated_df()
        assert "_tf_ema_fast" in df.columns

    def test_ema_slow_column_added(self) -> None:
        df = self._populated_df()
        assert "_tf_ema_slow" in df.columns

    def test_rsi_column_added(self) -> None:
        df = self._populated_df()
        assert "_tf_rsi" in df.columns

    def test_atr_column_added(self) -> None:
        df = self._populated_df()
        assert "_tf_atr" in df.columns

    def test_sma200_column_added_when_required(self) -> None:
        df = self._populated_df(require_sma200=True)
        assert "_tf_sma200" in df.columns

    def test_sma200_column_sentinel_when_disabled(self) -> None:
        df = self._populated_df(require_sma200=False)
        # Column should still exist (as sentinel 0.0) so signal code never KeyErrors
        assert "_tf_sma200" in df.columns
        assert (df["_tf_sma200"] == 0.0).all()

    def test_original_ohlcv_columns_preserved(self) -> None:
        df = self._populated_df()
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns

    def test_ema_fast_values_are_numeric(self) -> None:
        df = self._populated_df()
        assert df["_tf_ema_fast"].dtype in (np.float64, np.float32, float)

    def test_ema_slow_warm_up_first_rows_may_be_nan(self) -> None:
        """EMA(26) needs at least 26 rows; row 0 is typically NaN."""
        mod = _make_module()
        df = _make_trending_up_df(n=250)
        ctx = make_module_context()
        df = mod.populate_indicators(df, {}, ctx)
        # Last row must not be NaN
        assert not np.isnan(df["_tf_ema_slow"].iloc[-1])

    def test_rsi_bounded_0_to_100(self) -> None:
        df = self._populated_df()
        valid = df["_tf_rsi"].dropna()
        assert (valid >= 0.0).all() and (valid <= 100.0).all()

    def test_atr_non_negative(self) -> None:
        df = self._populated_df()
        valid = df["_tf_atr"].dropna()
        assert (valid >= 0.0).all()

    def test_pandas_fallback_ema(self) -> None:
        """When talib raises, pandas fallback produces valid EMA values."""
        mod = _make_module()
        df = _make_trending_up_df(n=100)
        ctx = make_module_context()
        with patch.dict("sys.modules", {"talib": None}):
            result = mod.populate_indicators(df.copy(), {}, ctx)
        assert "_tf_ema_fast" in result.columns
        assert not np.isnan(result["_tf_ema_fast"].iloc[-1])


# ===========================================================================
# TestEntrySignal
# ===========================================================================


class TestEntrySignal:
    """Entry signal logic in trade mode."""

    def _entry(
        self,
        ema_fast: float,
        ema_slow: float,
        rsi: float,
        atr_pct: float = 0.01,
        close: float = 100.0,
        require_sma200: bool = False,
        sma200: Optional[float] = None,
    ) -> ModuleSignal:
        mod = _make_module(
            mode="trade",
            require_above_sma200=require_sma200,
            atr_min_pct=0.005,
            rsi_entry_min=45.0,
            rsi_entry_max=70.0,
        )
        df = _make_flat_df(n=50, price=close)
        df = _inject_indicators(
            df,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi=rsi,
            sma200=sma200 if sma200 is not None else 0.0,
            atr=close * atr_pct,
        )
        # Override close on last row to the value we want
        df.iloc[-1, df.columns.get_loc("close")] = close
        ctx = make_module_context()
        return mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)

    def test_all_conditions_met_returns_enter_long_true(self) -> None:
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=55.0, close=150.0)
        assert sig.enter_long is True

    def test_entry_tag_prefixed_with_module_id(self) -> None:
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=55.0, close=150.0)
        assert sig.entry_tag is not None
        assert sig.entry_tag.startswith("trend_following_v1:")

    def test_downtrend_ema_fast_below_slow_no_entry(self) -> None:
        sig = self._entry(ema_fast=90.0, ema_slow=100.0, rsi=55.0, close=100.0)
        assert sig.enter_long is not True

    def test_rsi_below_minimum_no_entry(self) -> None:
        # RSI = 40, below default rsi_entry_min=45
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=40.0, close=100.0)
        assert sig.enter_long is not True

    def test_rsi_above_maximum_no_entry(self) -> None:
        # RSI = 72, above default rsi_entry_max=70
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=72.0, close=100.0)
        assert sig.enter_long is not True

    def test_rsi_at_boundary_min_no_entry(self) -> None:
        # RSI exactly at rsi_entry_min=45 — strict inequality required
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=45.0, close=100.0)
        assert sig.enter_long is not True

    def test_rsi_at_boundary_max_no_entry(self) -> None:
        # RSI exactly at rsi_entry_max=70 — strict inequality required
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=70.0, close=100.0)
        assert sig.enter_long is not True

    def test_insufficient_atr_no_entry(self) -> None:
        # ATR/close = 0.001 < atr_min_pct=0.005
        sig = self._entry(
            ema_fast=110.0, ema_slow=100.0, rsi=55.0, atr_pct=0.001, close=100.0
        )
        assert sig.enter_long is not True

    def test_sma200_gate_blocks_entry_when_price_below(self) -> None:
        # close=80, sma200=100 → price below SMA200 — blocked when gate enabled
        sig = self._entry(
            ema_fast=110.0,
            ema_slow=100.0,
            rsi=55.0,
            close=80.0,
            require_sma200=True,
            sma200=100.0,
        )
        assert sig.enter_long is not True

    def test_sma200_gate_allows_entry_when_price_above(self) -> None:
        # close=120, sma200=100 → price above SMA200 — passes
        sig = self._entry(
            ema_fast=130.0,
            ema_slow=120.0,
            rsi=55.0,
            close=120.0,
            require_sma200=True,
            sma200=100.0,
        )
        assert sig.enter_long is True

    def test_disabled_sma200_gate_does_not_block_entry(self) -> None:
        # require_above_sma200=False → no SMA gate → should enter
        sig = self._entry(
            ema_fast=110.0,
            ema_slow=100.0,
            rsi=55.0,
            close=50.0,
            require_sma200=False,
            sma200=9999.0,  # would fail if gate were active
        )
        assert sig.enter_long is True

    def test_empty_dataframe_returns_neutral_signal(self) -> None:
        mod = _make_module()
        empty_df = DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = make_module_context()
        sig = mod.generate_entry_signal(empty_df, {"pair": "BTC/USDT"}, ctx)
        assert sig.enter_long is None

    def test_nan_indicator_returns_neutral_signal(self) -> None:
        mod = _make_module()
        df = _make_flat_df(n=10)
        df["_tf_ema_fast"] = np.nan
        df["_tf_ema_slow"] = np.nan
        df["_tf_rsi"] = np.nan
        df["_tf_sma200"] = 0.0
        df["_tf_atr"] = np.nan
        ctx = make_module_context()
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert sig.enter_long is None

    def test_confidence_is_set_when_entry_fires(self) -> None:
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=55.0, close=150.0)
        assert sig.enter_long is True
        assert 0.0 < sig.confidence <= 1.0

    def test_metadata_contains_indicator_values(self) -> None:
        sig = self._entry(ema_fast=110.0, ema_slow=100.0, rsi=55.0, close=150.0)
        assert "rsi" in sig.metadata
        assert "atr_pct" in sig.metadata


# ===========================================================================
# TestExitSignal
# ===========================================================================


class TestExitSignal:
    """Exit signal logic."""

    def _exit(
        self,
        ema_fast: float,
        ema_slow: float,
        rsi: float,
        close: float = 100.0,
        trail_stop_pct: float = 0.03,
    ) -> ModuleSignal:
        mod = _make_module(
            mode="trade",
            rsi_exit_overbought=75.0,
            trail_stop_pct=trail_stop_pct,
        )
        df = _make_flat_df(n=50, price=close)
        df = _inject_indicators(df, ema_fast=ema_fast, ema_slow=ema_slow, rsi=rsi)
        df.iloc[-1, df.columns.get_loc("close")] = close
        ctx = make_module_context()
        return mod.generate_exit_signal(df, {"pair": "BTC/USDT"}, ctx)

    def test_ema_reversal_triggers_exit(self) -> None:
        sig = self._exit(ema_fast=95.0, ema_slow=100.0, rsi=55.0)
        assert sig.exit_long is True
        assert sig.exit_tag is not None
        assert "ema_reversal" in sig.exit_tag

    def test_exit_tag_prefixed_with_module_id(self) -> None:
        sig = self._exit(ema_fast=95.0, ema_slow=100.0, rsi=55.0)
        assert sig.exit_tag.startswith("trend_following_v1:")

    def test_rsi_overbought_triggers_exit(self) -> None:
        # RSI = 80 > rsi_exit_overbought=75
        sig = self._exit(ema_fast=105.0, ema_slow=100.0, rsi=80.0)
        assert sig.exit_long is True
        assert sig.exit_tag is not None
        assert "rsi_overbought" in sig.exit_tag

    def test_trailing_stop_breach_triggers_exit(self) -> None:
        # ema_slow=100, trail_stop_pct=0.03 → stop_level=97.0; close=95 < 97 → exit
        sig = self._exit(
            ema_fast=105.0,
            ema_slow=100.0,
            rsi=55.0,
            close=95.0,
            trail_stop_pct=0.03,
        )
        assert sig.exit_long is True
        assert sig.exit_tag is not None
        assert "trail_stop" in sig.exit_tag

    def test_trail_stop_level_exactly_at_boundary_no_exit(self) -> None:
        # close == stop_level exactly → NOT a breach (strict <)
        # ema_slow=100, trail_stop_pct=0.03 → stop_level=97.0; close=97.0 → NO exit
        sig = self._exit(
            ema_fast=105.0,
            ema_slow=100.0,
            rsi=55.0,
            close=97.0,
            trail_stop_pct=0.03,
        )
        # Either no exit or exit; implementation uses strict < so 97.0 should NOT exit
        assert sig.exit_long is not True

    def test_healthy_trend_returns_exit_long_false(self) -> None:
        # EMA cross bullish, RSI healthy, price well above trail stop
        sig = self._exit(ema_fast=110.0, ema_slow=100.0, rsi=55.0, close=105.0)
        assert sig.exit_long is False

    def test_empty_dataframe_returns_neutral_signal(self) -> None:
        mod = _make_module()
        empty_df = DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = make_module_context()
        sig = mod.generate_exit_signal(empty_df, {"pair": "BTC/USDT"}, ctx)
        assert sig.exit_long is None

    def test_nan_indicators_return_neutral_signal(self) -> None:
        mod = _make_module()
        df = _make_flat_df(n=10)
        df["_tf_ema_fast"] = np.nan
        df["_tf_ema_slow"] = np.nan
        df["_tf_rsi"] = np.nan
        ctx = make_module_context()
        sig = mod.generate_exit_signal(df, {}, ctx)
        assert sig.exit_long is None

    def test_exit_metadata_contains_reason(self) -> None:
        sig = self._exit(ema_fast=95.0, ema_slow=100.0, rsi=55.0)
        assert "reason" in sig.metadata
        assert sig.metadata["reason"] == "ema_reversal"


# ===========================================================================
# TestAlertMode
# ===========================================================================


class TestAlertMode:
    """Verify alert-mode behaviour: no trade entry, alert emitted."""

    def _conditions_met_df(self, close: float = 150.0) -> DataFrame:
        df = _make_flat_df(n=50, price=close)
        return _inject_indicators(
            df,
            ema_fast=close * 1.05,  # fast > slow: uptrend
            ema_slow=close,
            rsi=55.0,               # inside [45, 70]
            sma200=0.0,             # gate disabled sentinel
            atr=close * 0.01,       # > atr_min_pct=0.005
        )

    def test_alert_mode_entry_conditions_met_returns_enter_long_false(self) -> None:
        mod = _make_module(mode="alert", require_above_sma200=False, atr_min_pct=0.005)
        df = self._conditions_met_df()
        ctx = make_module_context()
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert sig.enter_long is False

    def test_alert_mode_sends_alert_exactly_once(self) -> None:
        mod = _make_module(mode="alert", require_above_sma200=False, atr_min_pct=0.005)
        mock_metrics = MagicMock()
        mod._metrics = mock_metrics
        df = self._conditions_met_df()
        ctx = make_module_context()
        mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        mock_metrics.send_alert.assert_called_once()

    def test_alert_mode_send_alert_receives_correct_module_id(self) -> None:
        mod = _make_module(mode="alert", require_above_sma200=False, atr_min_pct=0.005)
        mock_metrics = MagicMock()
        mod._metrics = mock_metrics
        df = self._conditions_met_df()
        ctx = make_module_context()
        mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        call_kwargs = mock_metrics.send_alert.call_args
        # Accept both positional and keyword call styles
        module_id_arg = (
            call_kwargs.kwargs.get("module_id")
            or (call_kwargs.args[0] if call_kwargs.args else None)
        )
        assert module_id_arg == "trend_following_v1"

    def test_alert_mode_no_alert_when_conditions_not_met(self) -> None:
        mod = _make_module(mode="alert", require_above_sma200=False)
        mock_metrics = MagicMock()
        mod._metrics = mock_metrics
        # Downtrend — conditions NOT met
        df = _make_flat_df(n=50, price=100.0)
        df = _inject_indicators(
            df,
            ema_fast=90.0,  # fast < slow: downtrend
            ema_slow=100.0,
            rsi=55.0,
        )
        ctx = make_module_context()
        mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        mock_metrics.send_alert.assert_not_called()

    def test_trade_mode_same_conditions_returns_enter_long_true(self) -> None:
        mod = _make_module(mode="trade", require_above_sma200=False, atr_min_pct=0.005)
        df = self._conditions_met_df()
        ctx = make_module_context()
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert sig.enter_long is True

    def test_alert_mode_metadata_contains_mode_key(self) -> None:
        mod = _make_module(mode="alert", require_above_sma200=False, atr_min_pct=0.005)
        mod._metrics = MagicMock()
        df = self._conditions_met_df()
        ctx = make_module_context()
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert sig.metadata.get("mode") == "alert"


# ===========================================================================
# TestGetModuleState
# ===========================================================================


class TestGetModuleState:
    """get_module_state returns correct serialisable structure."""

    def test_returns_dict(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("BTC/USDT")
        assert isinstance(state, dict)

    def test_contains_module_id(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("BTC/USDT")
        assert state["module_id"] == "trend_following_v1"

    def test_contains_pair(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("ETH/USDT")
        assert state["pair"] == "ETH/USDT"

    def test_contains_mode(self) -> None:
        mod = _make_module(mode="alert")
        state = mod.get_module_state("BTC/USDT")
        assert state["mode"] == "alert"

    def test_contains_version(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("BTC/USDT")
        assert "version" in state

    def test_contains_in_position_flag(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("BTC/USDT")
        assert "in_position" in state

    def test_state_is_json_serialisable(self) -> None:
        import json
        mod = _make_module()
        state = mod.get_module_state("BTC/USDT")
        # Must not raise
        json.dumps(state)

    def test_unknown_pair_returns_default_state(self) -> None:
        mod = _make_module()
        state = mod.get_module_state("XRP/USDT")
        assert state["pair"] == "XRP/USDT"
        assert state["in_position"] is False


# ===========================================================================
# TestResetModuleState
# ===========================================================================


class TestResetModuleState:
    """reset_module_state clears per-pair bookkeeping."""

    def test_reset_removes_pair_from_state(self) -> None:
        mod = _make_module()
        mod._pair_state["BTC/USDT"] = {"entry_price": 50_000.0, "in_position": True}
        mod.reset_module_state("BTC/USDT")
        assert "BTC/USDT" not in mod._pair_state

    def test_reset_unknown_pair_does_not_raise(self) -> None:
        mod = _make_module()
        # Must be idempotent — no error on unknown pair
        mod.reset_module_state("DOGE/USDT")

    def test_reset_only_removes_target_pair(self) -> None:
        mod = _make_module()
        mod._pair_state["BTC/USDT"] = {"entry_price": 1.0, "in_position": False}
        mod._pair_state["ETH/USDT"] = {"entry_price": 2.0, "in_position": False}
        mod.reset_module_state("BTC/USDT")
        assert "ETH/USDT" in mod._pair_state


# ===========================================================================
# TestLifecycleHooks
# ===========================================================================


class TestLifecycleHooks:
    """on_bot_start, shutdown, adjust_position, on_order_filled."""

    def test_on_bot_start_populates_whitelist_pairs(self) -> None:
        mod = _make_module()
        ctx = make_module_context(pair="BTC/USDT", whitelist=["BTC/USDT", "ETH/USDT"])
        mod.on_bot_start(ctx)
        assert "BTC/USDT" in mod._pair_state
        assert "ETH/USDT" in mod._pair_state

    def test_on_bot_start_does_not_overwrite_existing_pair_state(self) -> None:
        mod = _make_module()
        mod._pair_state["BTC/USDT"] = {"entry_price": 99.0, "in_position": True}
        ctx = make_module_context(pair="BTC/USDT", whitelist=["BTC/USDT"])
        mod.on_bot_start(ctx)
        # Must NOT clobber existing state
        assert mod._pair_state["BTC/USDT"]["entry_price"] == pytest.approx(99.0)
        assert mod._pair_state["BTC/USDT"]["in_position"] is True

    def test_shutdown_clears_all_pair_state(self) -> None:
        mod = _make_module()
        mod._pair_state["BTC/USDT"] = {"entry_price": 1.0, "in_position": False}
        mod._pair_state["ETH/USDT"] = {"entry_price": 2.0, "in_position": True}
        ctx = make_module_context()
        mod.shutdown(ctx)
        assert len(mod._pair_state) == 0

    def test_adjust_position_always_returns_none(self) -> None:
        mod = _make_module()
        ctx = make_module_context()
        result = mod.adjust_position(
            MagicMock(), None, 50_000.0, 0.05, None, 1000.0, ctx
        )
        assert result is None

    def test_on_order_filled_does_not_raise(self) -> None:
        mod = _make_module()
        ctx = make_module_context()
        # Must not raise — it's a no-op
        mod.on_order_filled("BTC/USDT", MagicMock(), MagicMock(), None, ctx)

    def test_shutdown_is_idempotent(self) -> None:
        mod = _make_module()
        ctx = make_module_context()
        mod.shutdown(ctx)
        # Second shutdown must not raise
        mod.shutdown(ctx)


# ===========================================================================
# TestPandasFallbackIndicators
# ===========================================================================


class TestPandasFallbackIndicators:
    """Verify that the standalone pandas helper functions produce sane values."""

    def test_ema_pandas_same_length_as_input(self) -> None:
        s = pd.Series(np.arange(1.0, 51.0))
        result = _ema_pandas(s, 12)
        assert len(result) == len(s)

    def test_rsi_pandas_bounded(self) -> None:
        # Strictly upward series — RSI should converge toward high values
        s = pd.Series(np.arange(1.0, 101.0), dtype=float)
        result = _rsi_pandas(s, 14)
        valid = result.dropna()
        assert (valid >= 0.0).all()
        assert (valid <= 100.0).all()

    def test_atr_pandas_non_negative(self) -> None:
        n = 50
        close = pd.Series(np.linspace(100.0, 110.0, n))
        high = close + 1.0
        low = close - 1.0
        result = _atr_pandas(high, low, close, 14)
        valid = result.dropna()
        assert (valid >= 0.0).all()

    def test_sma_pandas_rolling_mean(self) -> None:
        s = pd.Series([float(i) for i in range(1, 11)])
        result = _sma_pandas(s, 3)
        # SMA(3) at position 2 (0-indexed) = (1+2+3)/3 = 2.0
        assert result.iloc[2] == pytest.approx(2.0)
        # First two values should be NaN (min_periods=3)
        assert np.isnan(result.iloc[0])
        assert np.isnan(result.iloc[1])

"""
tests/test_integration_optimizer_to_module.py
=============================================
Integration tests for the full pipeline:
    OptunaGridOptimizer.inject_into_shared_state()
        
 SharedState
        
 GridTradingModule._resolve_config()
        
 GridTradingModule._initialize_grid()

These tests exercise the real data-flow path using a real SharedState
(in-memory, temp-file persistence so disk I/O is harmless in CI).
No production code is stubbed unless the stub is a *test-data* factory
(e.g. a fake _StudyRecord injected directly into the optimizer's cache).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Make algo_system importable without freqtrade installed
# ---------------------------------------------------------------------------
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.config.grid_config import GridConfig
from algo_system.modules.grid_trading.grid_trading_module import GridTradingModule
from algo_system.orchestrator.optuna_grid_optimizer import (
    OptunaGridOptimizer,
    _StudyRecord,
)
from algo_system.orchestrator.shared_state import SharedState

from test_helpers import make_module_context


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _make_shared_state() -> SharedState:
    """Fresh in-memory SharedState backed by a temp file (avoids disk clutter)."""
    return SharedState(
        persistence_path=os.path.join(
            tempfile.gettempdir(), "lats_test_integration_opt_to_mod.json"
        )
    )


def _make_initialized_module(
    shared_state: SharedState,
    grid_count: int = 5,
    initial_stake: float = 500.0,
    pair: str = "BTC/USDT",
    whitelist: Optional[list] = None,
) -> GridTradingModule:
    """
    Return a fully initialized GridTradingModule whose SharedState already
    contains the v1 config blob.  Pattern mirrors _make_initialized_module
    in test_grid_module.py.
    """
    shared_state.set(
        "grid_trading_v1",
        "config",
        {
            "upper_bound_pct": 0.05,
            "lower_bound_pct": 0.05,
            "grid_count": grid_count,
            "initial_stake": initial_stake,
        },
    )
    ctx = make_module_context(
        pair=pair,
        whitelist=whitelist or [pair],
        shared_state=shared_state,
    )
    mod = GridTradingModule()
    mod.initialize(ctx)
    return mod


def _make_linear_config(
    grid_distance: float = 0.01,
    grid_range: float = 0.10,
) -> GridConfig:
    """Return a simple linear (non-log, non-auto-adjust) GridConfig."""
    return GridConfig(
        grid_distance=grid_distance,
        grid_range=grid_range,
        auto_adjust=False,
        log_scale=False,
        method="market_price",
        dynamic_midprice=False,
    )


def _inject_study_record(
    optimizer: OptunaGridOptimizer,
    symbol: str,
    year: int,
    cfg: GridConfig,
) -> None:
    """
    Bypass SQLite and inject a synthetic _StudyRecord directly into the
    optimizer's cache.  This lets integration tests run without a real
    Optuna DB file.
    """
    record = _StudyRecord(
        study_name=f"{symbol}_{year}_study",
        symbol=symbol,
        year=year,
        best_value=0.42,
        n_trials=50,
        best_params=cfg.to_dict(),
    )
    optimizer._cache[(symbol.upper(), year)] = record
    optimizer._loaded = True


# ===========================================================================
# TestOptimizerToModulePipeline
# ===========================================================================


class TestOptimizerToModulePipeline:
    """Full-pipeline integration: optimizer 
 shared_state 
 module."""

    # ------------------------------------------------------------------
    # 1. inject_then_resolve_config
    # ------------------------------------------------------------------

    def test_inject_then_resolve_config(self) -> None:
        """
        SharedState.set() with a GridConfig dict must be round-tripped
        back to an identical GridConfig by _resolve_config().
        """
        ss = _make_shared_state()
        original = _make_linear_config(grid_distance=0.01, grid_range=0.10)

        # Step 1 
 write the config into SharedState exactly as the optimizer would
        ss.set("grid_trading_v1", "optuna:BTC", original.to_dict())

        # Step 2 
 build a module and call _resolve_config
        mod = _make_initialized_module(ss, pair="BTC/USDT")
        ctx = make_module_context(pair="BTC/USDT", shared_state=ss)

        result = mod._resolve_config("BTC/USDT", ctx)

        assert result is not None, "_resolve_config must find the entry in SharedState"
        assert result.grid_distance == pytest.approx(original.grid_distance)
        assert result.grid_range == pytest.approx(original.grid_range)
        assert result.auto_adjust == original.auto_adjust
        assert result.log_scale == original.log_scale
        assert result.method == original.method

    # Backward-compat alias test (old name _resolve_v2_config)
    def test_inject_then_resolve_v2_config(self) -> None:
        """Alias: _resolve_v2_config has been renamed to _resolve_config."""
        ss = _make_shared_state()
        original = _make_linear_config(grid_distance=0.01, grid_range=0.10)
        ss.set("grid_trading_v1", "optuna:BTC", original.to_dict())
        mod = _make_initialized_module(ss, pair="BTC/USDT")
        ctx = make_module_context(pair="BTC/USDT", shared_state=ss)

        # _resolve_config is the canonical name now
        result = mod._resolve_config("BTC/USDT", ctx)
        assert result is not None
        assert isinstance(result, GridConfig)
        assert result.grid_distance == pytest.approx(original.grid_distance)
        assert result.grid_range == pytest.approx(original.grid_range)
        assert result.auto_adjust == original.auto_adjust
        assert result.log_scale == original.log_scale
        assert result.method == original.method

    # ------------------------------------------------------------------
    # 2. config_survives_key_format_normalization
    # ------------------------------------------------------------------

    def test_v2_config_survives_key_format_normalization(self) -> None:
        """
        Config stored under "optuna:TSLA" must be found for the full pair
        "TSLA.US/USD" because _resolve_config strips the exchange suffix
        via pair.split("/")[0].split(".")[0].
        """
        ss = _make_shared_state()
        cfg = _make_linear_config()

        # Write under the bare-symbol key (no exchange suffix)
        ss.set("grid_trading_v1", "optuna:TSLA", cfg.to_dict())

        mod = _make_initialized_module(ss, pair="TSLA.US/USD")
        ctx = make_module_context(pair="TSLA.US/USD", shared_state=ss)

        # The pair has an exchange suffix 
 the module must still find the config
        result = mod._resolve_config("TSLA.US/USD", ctx)

        assert result is not None, (
            "_resolve_config should normalise 'TSLA.US/USD' 
 'TSLA' "
            "and find the 'optuna:TSLA' entry"
        )
        assert isinstance(result, GridConfig)

    # ------------------------------------------------------------------
    # 3. v2_grid_initialized_via_adjust_position
    # ------------------------------------------------------------------

    def test_v2_grid_initialized_via_adjust_position(self) -> None:
        """
        When SharedState contains a linear GridConfig, _initialize_grid must:
          - return a non-None GridState
          - produce at least 2 grid levels
          - place all levels within [midprice*(1 - grid_range/2),
                                     midprice*(1 + grid_range/2)]  (approximately)
        """
        ss = _make_shared_state()
        grid_distance = 0.01   # 1 %
        grid_range = 0.10      # 10 %
        cfg = _make_linear_config(
            grid_distance=grid_distance,
            grid_range=grid_range,
        )
        ss.set("grid_trading_v1", "optuna:BTC", cfg.to_dict())

        mod = _make_initialized_module(ss, pair="BTC/USDT")
        ctx = make_module_context(pair="BTC/USDT", shared_state=ss)

        # Manually resolve config (mirrors what _initialize_grid does lazily)
        loaded_cfg = mod._resolve_config("BTC/USDT", ctx)
        assert loaded_cfg is not None

        current_rate = 50_000.0
        state = mod._initialize_grid("BTC/USDT", current_rate=current_rate, v2_cfg=loaded_cfg)

        assert state is not None, "_initialize_grid must return a GridState, not None"
        assert len(state.grid_levels) >= 2, (
            "v2 grid must have at least 2 levels"
        )

        # Verify levels are approximately within the expected range
        expected_lower = current_rate * (1.0 - grid_range / 2.0)
        expected_upper = current_rate * (1.0 + grid_range / 2.0)
        # np.arange(start, stop, step) 
 last level < stop, so use a small tolerance
        tolerance = grid_distance * current_rate * 2.0  # 2 step widths of slack
        for level in state.grid_levels:
            assert level >= expected_lower - tolerance, (
                f"Level {level:.2f} is below expected lower bound "
                f"{expected_lower - tolerance:.2f}"
            )
            assert level <= expected_upper + tolerance, (
                f"Level {level:.2f} is above expected upper bound "
                f"{expected_upper + tolerance:.2f}"
            )

    # ------------------------------------------------------------------
    # 4. log_scale_with_df_produces_log_spaced_levels
    # ------------------------------------------------------------------

    def test_v2_fallback_to_v1_when_log_scale_true(self) -> None:
        """
        When GridConfig.log_scale=True and no DataFrame is available
        (via _initialize_grid compat shim), fall back to the v1 linear grid
        rather than returning None.  The resulting GridState must not be None.
        """
        ss = _make_shared_state()
        log_scale_cfg = GridConfig(
            auto_adjust=False,
            log_scale=True,   # triggers fallback inside _initialize_grid
            grid_distance=0.01,
            grid_range=0.10,
            method="market_price",
            dynamic_midprice=False,
        )

        mod = _make_initialized_module(ss, pair="ETH/USDT")

        current_rate = 3_000.0
        state = mod._initialize_grid("ETH/USDT", current_rate=current_rate, v2_cfg=log_scale_cfg)

        assert state is not None, (
            "_initialize_grid MUST NOT return None when log_scale=True "
            "without a DataFrame 
 it must fall back to the v1 linear grid"
        )
        assert len(state.grid_levels) >= 2, "fallback v1 grid must have at least 2 levels"

        # Verify the fallback used the v1 
5 % bounds from module config
        upper_pct = mod._config["upper_bound_pct"]   # 0.05
        lower_pct = mod._config["lower_bound_pct"]   # 0.05
        expected_lower = current_rate * (1.0 - lower_pct)
        expected_upper = current_rate * (1.0 + upper_pct)

        # v1 levels are linearly spaced between expected_lower and expected_upper
        assert min(state.grid_levels) == pytest.approx(expected_lower, rel=1e-6)
        assert max(state.grid_levels) == pytest.approx(expected_upper, rel=1e-6)

    # ------------------------------------------------------------------
    # 5. inject_into_shared_state_key_format
    # ------------------------------------------------------------------

    def test_inject_into_shared_state_key_format(self) -> None:
        """
        OptunaGridOptimizer.inject_into_shared_state() must write each
        symbol's best config under (module_id, "optuna:{SYMBOL}") with
        a dict payload that round-trips through GridConfig.from_dict().
        """
        ss = _make_shared_state()

        # Build an optimizer that bypasses SQLite by injecting a fake study record
        optimizer = OptunaGridOptimizer(db_path="/nonexistent/path/study.db")
        cfg = _make_linear_config(grid_distance=0.02, grid_range=0.15)
        _inject_study_record(optimizer, symbol="AAPL", year=2024, cfg=cfg)

        # Call the real inject_into_shared_state 
 the method under test
        optimizer.inject_into_shared_state(ss, module_id="grid_trading_v1")

        # Verify the entry is present
        entry = ss.get("grid_trading_v1", "optuna:AAPL")
        assert entry is not None, (
            "inject_into_shared_state must write an entry for AAPL "
            "under key ('grid_trading_v1', 'optuna:AAPL')"
        )

        # Verify the payload is a dict with the expected GridConfig fields
        data = entry["data"]
        assert isinstance(data, dict), "entry['data'] must be a dict"
        assert "grid_distance" in data, "data must contain 'grid_distance'"
        assert "grid_range" in data, "data must contain 'grid_range'"
        assert "auto_adjust" in data, "data must contain 'auto_adjust'"
        assert "log_scale" in data, "data must contain 'log_scale'"
        assert "method" in data, "data must contain 'method'"

        # Verify full deserialization succeeds
        reconstructed = GridConfig.from_dict(data)
        assert isinstance(reconstructed, GridConfig)
        assert reconstructed.grid_distance == pytest.approx(cfg.grid_distance)
        assert reconstructed.grid_range == pytest.approx(cfg.grid_range)
        assert reconstructed.auto_adjust == cfg.auto_adjust
        assert reconstructed.log_scale == cfg.log_scale
        assert reconstructed.method == cfg.method

    # ------------------------------------------------------------------
    # Additional edge-case: multiple symbols injected 
 all retrievable
    # ------------------------------------------------------------------

    def test_inject_multiple_symbols(self) -> None:
        """
        inject_into_shared_state with multiple symbols in cache must write
        a separate entry for each symbol and all must be retrievable.
        """
        ss = _make_shared_state()
        optimizer = OptunaGridOptimizer(db_path="/nonexistent/path/study.db")

        symbols_and_cfgs = {
            "AAPL": _make_linear_config(grid_distance=0.01, grid_range=0.10),
            "GOOG": _make_linear_config(grid_distance=0.02, grid_range=0.20),
            "MSFT": _make_linear_config(grid_distance=0.005, grid_range=0.05),
        }
        for symbol, cfg in symbols_and_cfgs.items():
            _inject_study_record(optimizer, symbol=symbol, year=2024, cfg=cfg)

        optimizer.inject_into_shared_state(ss, module_id="grid_trading_v1")

        for symbol, original_cfg in symbols_and_cfgs.items():
            entry = ss.get("grid_trading_v1", f"optuna:{symbol}")
            assert entry is not None, f"Entry missing for symbol {symbol}"
            result = GridConfig.from_dict(entry["data"])
            assert result.grid_distance == pytest.approx(original_cfg.grid_distance), (
                f"grid_distance mismatch for {symbol}"
            )
            assert result.grid_range == pytest.approx(original_cfg.grid_range), (
                f"grid_range mismatch for {symbol}"
            )

    # ------------------------------------------------------------------
    # Edge-case: resolve returns None when no optuna key is present
    # ------------------------------------------------------------------

    def test_resolve_returns_none_when_no_optuna_key(self) -> None:
        """
        _resolve_config must return None (not raise) when SharedState
        does not have an entry for the given pair.
        """
        ss = _make_shared_state()
        # Deliberately do NOT write any "optuna:ETH" key
        mod = _make_initialized_module(ss, pair="ETH/USDT")
        ctx = make_module_context(pair="ETH/USDT", shared_state=ss)

        result = mod._resolve_config("ETH/USDT", ctx)
        assert result is None

    # ------------------------------------------------------------------
    # Edge-case: corrupted data in SharedState does not raise
    # ------------------------------------------------------------------

    def test_resolve_gracefully_handles_corrupt_data(self) -> None:
        """
        _resolve_config must return None (not raise) when the SharedState
        entry's data is malformed / cannot be deserialised into GridConfig.
        """
        ss = _make_shared_state()
        # Write garbage that will fail GridConfig.from_dict()
        ss.set("grid_trading_v1", "optuna:ETH", {"method": "NOT_A_VALID_METHOD"})

        mod = _make_initialized_module(ss, pair="ETH/USDT")
        ctx = make_module_context(pair="ETH/USDT", shared_state=ss)

        # Must not raise 
 must log a warning and return None
        result = mod._resolve_config("ETH/USDT", ctx)
        assert result is None, (
            "_resolve_config must return None for undeserializable data, not raise"
        )

"""
tests/test_grid_module.py
Unit tests for GridConfig, GridCalculator, GridState, and GridTradingModule
under the unified 003 API.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
_TESTS_DIR = Path(__file__).resolve().parent       # algo_system/tests/
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.config.grid_config import GridConfig
from algo_system.modules.grid_trading.grid_calculator import GridCalculator
from algo_system.modules.grid_trading.grid_state import GridState  # type: ignore[import]
from algo_system.modules.grid_trading.grid_trading_module import GridTradingModule  # type: ignore[import]

from test_helpers import MockTrade, make_module_context, make_ohlcv_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _linear_levels(lower: float, upper: float, count: int) -> list[float]:
    """Build a list of `count` evenly-spaced levels between `lower` and `upper` inclusive.

    Used as a test fixture for crossing-detection tests where we need an
    exact, predictable level list (not the np.arange end-exclusive one
    `_generate_linear_levels` produces).
    """
    if count < 2:
        raise ValueError("count must be >= 2")
    step = (upper - lower) / (count - 1)
    return [lower + step * i for i in range(count)]


# ===========================================================================
# GridConfig validation (replaces V1 validate_grid)
# ===========================================================================

class TestGridConfigValidation:
    def test_default_validates(self) -> None:
        GridConfig().validate()  # should not raise

    def test_grid_distance_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            GridConfig(grid_distance=0)

    def test_grid_range_le_distance_raises(self) -> None:
        with pytest.raises(ValueError):
            GridConfig(grid_distance=0.10, grid_range=0.10)

    def test_bad_method_raises(self) -> None:
        with pytest.raises(ValueError):
            GridConfig(method="zigzag")

    def test_bad_indicator_raises(self) -> None:
        with pytest.raises(ValueError):
            GridConfig(selected_indicators=["NOT_AN_INDICATOR"])


# ===========================================================================
# GridCalculator level generation (linear + log)
# ===========================================================================

class TestGridCalculatorLevels:
    def test_linear_levels_count_consistent_with_step(self) -> None:
        # span 0.10, step 0.02 -> floor(0.10/0.02) = 5 levels via np.arange
        cfg = GridConfig(grid_distance=0.02, grid_range=0.10)
        levels = GridCalculator.generate_levels(cfg, midprice=100.0)
        assert len(levels) == 5

    def test_linear_levels_centred_on_midprice(self) -> None:
        cfg = GridConfig(grid_distance=0.02, grid_range=0.10)
        levels = GridCalculator.generate_levels(cfg, midprice=100.0)
        # Levels span [midprice - range/2, midprice + range/2)
        assert levels[0] >= 100.0 - 0.10 / 2 - 1e-9
        assert levels[-1] < 100.0 + 0.10 / 2

    def test_linear_levels_even_spacing(self) -> None:
        cfg = GridConfig(grid_distance=0.02, grid_range=0.20)
        levels = GridCalculator.generate_levels(cfg, midprice=100.0)
        spacings = [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]
        assert all(abs(s - spacings[0]) < 1e-9 for s in spacings)

    def test_log_scale_requires_df(self) -> None:
        cfg = GridConfig(log_scale=True)
        with pytest.raises(ValueError):
            GridCalculator.generate_levels(cfg, midprice=100.0)

    def test_log_scale_with_df_produces_levels(self) -> None:
        cfg = GridConfig(log_scale=True)
        df = make_ohlcv_df(100)
        levels = GridCalculator.generate_levels(cfg, midprice=50000.0, df=df)
        assert len(levels) >= 2
        assert levels == sorted(levels)

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError):
            GridCalculator._generate_linear_levels(midprice=float("nan"), grid_distance=0.01, grid_range=0.10)
        with pytest.raises(ValueError):
            GridCalculator._generate_linear_levels(midprice=100.0, grid_distance=float("inf"), grid_range=0.10)


# ===========================================================================
# Crossing detection (renamed from V1: drop `get_` prefix, kw `filled`)
# ===========================================================================

class TestGridCalculatorCrossedDown:
    def _levels(self) -> list:
        return _linear_levels(90.0, 110.0, 5)  # [90, 95, 100, 105, 110]

    def test_no_crossing_when_price_flat(self) -> None:
        levels = self._levels()
        assert GridCalculator.levels_crossed_down(100.0, 100.0, levels) == []

    def test_no_crossing_when_price_rises(self) -> None:
        levels = self._levels()
        assert GridCalculator.levels_crossed_down(95.0, 100.0, levels) == []

    def test_single_level_crossed(self) -> None:
        levels = self._levels()
        crossed = GridCalculator.levels_crossed_down(101.0, 99.0, levels)
        assert crossed == [100.0]

    def test_multiple_levels_crossed_sorted_desc(self) -> None:
        levels = self._levels()
        crossed = GridCalculator.levels_crossed_down(106.0, 94.0, levels)
        assert crossed == [105.0, 100.0, 95.0]
        assert crossed == sorted(crossed, reverse=True)

    def test_boundary_exact_to_price(self) -> None:
        levels = self._levels()
        # to_price == level should include that level (to_price <= level)
        crossed = GridCalculator.levels_crossed_down(101.0, 100.0, levels)
        assert 100.0 in crossed

    def test_boundary_exact_from_price(self) -> None:
        levels = self._levels()
        # level == from_price should NOT be included (level < from_price)
        crossed = GridCalculator.levels_crossed_down(100.0, 95.0, levels)
        assert 100.0 not in crossed


class TestGridCalculatorCrossedUp:
    def _levels(self) -> list:
        return _linear_levels(90.0, 110.0, 5)

    def test_no_crossing_when_price_flat(self) -> None:
        levels = self._levels()
        assert GridCalculator.levels_crossed_up(100.0, 100.0, levels, filled={100.0}) == []

    def test_no_crossing_when_price_falls(self) -> None:
        levels = self._levels()
        assert GridCalculator.levels_crossed_up(100.0, 95.0, levels, filled={100.0}) == []

    def test_no_crossing_unfilled_level(self) -> None:
        levels = self._levels()
        assert GridCalculator.levels_crossed_up(99.0, 101.0, levels, filled=set()) == []

    def test_filled_none_returns_empty(self) -> None:
        levels = self._levels()
        # filled defaults to None -> no sells
        assert GridCalculator.levels_crossed_up(99.0, 101.0, levels) == []

    def test_single_filled_level_crossed(self) -> None:
        levels = self._levels()
        crossed = GridCalculator.levels_crossed_up(99.0, 101.0, levels, filled={100.0})
        assert crossed == [100.0]

    def test_multiple_filled_levels_crossed_sorted_asc(self) -> None:
        levels = self._levels()
        filled = {95.0, 100.0, 105.0}
        crossed = GridCalculator.levels_crossed_up(94.0, 106.0, levels, filled=filled)
        assert crossed == [95.0, 100.0, 105.0]
        assert crossed == sorted(crossed)

    def test_only_filled_subset_returned(self) -> None:
        levels = self._levels()
        filled = {95.0, 105.0}
        crossed = GridCalculator.levels_crossed_up(94.0, 106.0, levels, filled=filled)
        assert 100.0 not in crossed
        assert 95.0 in crossed and 105.0 in crossed


class TestGridCalculatorNavigation:
    def test_nearest_level(self) -> None:
        levels = [90.0, 95.0, 100.0, 105.0, 110.0]
        assert GridCalculator.nearest_level(96.0, levels) == 95.0
        assert GridCalculator.nearest_level(102.0, levels) == 100.0

    def test_nearest_level_empty(self) -> None:
        assert GridCalculator.nearest_level(100.0, []) is None

    def test_level_below(self) -> None:
        levels = [90.0, 95.0, 100.0, 105.0]
        assert GridCalculator.level_below(97.0, levels) == 95.0
        assert GridCalculator.level_below(90.0, levels) is None

    def test_level_above(self) -> None:
        levels = [90.0, 95.0, 100.0, 105.0]
        assert GridCalculator.level_above(97.0, levels) == 100.0
        assert GridCalculator.level_above(105.0, levels) is None


# ===========================================================================
# GridState tests (no more grid_count constructor arg; @property now)
# ===========================================================================

class TestGridState:
    def _make_state(self) -> GridState:
        levels = _linear_levels(45_000.0, 55_000.0, 5)
        return GridState(
            pair="BTC/USDT",
            upper_bound=55_000.0,
            lower_bound=45_000.0,
            grid_levels=levels,
            filled_levels=set(),
        )

    def test_grid_count_is_property(self) -> None:
        s = self._make_state()
        assert s.grid_count == 5  # derived from len(grid_levels)

    def test_initial_state_inactive(self) -> None:
        s = self._make_state()
        assert not s.is_active()  # last_price is None

    def test_update_last_price_activates(self) -> None:
        s = self._make_state()
        s.update_last_price(50_000.0)
        assert s.last_price == 50_000.0
        assert s.is_active()

    def test_mark_filled(self) -> None:
        s = self._make_state()
        level = s.grid_levels[2]
        s.mark_filled(level)
        assert s.is_level_filled(level)

    def test_mark_unfilled(self) -> None:
        s = self._make_state()
        level = s.grid_levels[1]
        s.mark_filled(level)
        s.mark_unfilled(level)
        assert not s.is_level_filled(level)

    def test_is_in_range(self) -> None:
        s = self._make_state()
        assert s.is_in_range(50_000.0)
        assert not s.is_in_range(44_000.0)
        assert not s.is_in_range(56_000.0)

    def test_get_unfilled_levels(self) -> None:
        s = self._make_state()
        s.mark_filled(s.grid_levels[0])
        unfilled = s.get_unfilled_levels()
        assert s.grid_levels[0] not in unfilled
        assert len(unfilled) == s.grid_count - 1

    def test_validation_upper_leq_lower_raises(self) -> None:
        levels = _linear_levels(90.0, 110.0, 5)
        with pytest.raises(ValueError):
            GridState(
                pair="X",
                upper_bound=80.0,
                lower_bound=90.0,
                grid_levels=levels,
                filled_levels=set(),
            )

    def test_serialisation_roundtrip(self) -> None:
        s = self._make_state()
        s.update_last_price(50_000.0)
        s.mark_filled(s.grid_levels[1])
        restored = GridState.from_dict(s.to_dict())
        assert restored.pair == s.pair
        assert restored.grid_levels == s.grid_levels
        assert restored.filled_levels == s.filled_levels
        assert restored.last_price == 50_000.0
        assert restored.grid_count == 5

    def test_update_last_price_rejects_nan(self) -> None:
        s = self._make_state()
        with pytest.raises(ValueError):
            s.update_last_price(float("nan"))

    def test_update_last_price_rejects_inf(self) -> None:
        s = self._make_state()
        with pytest.raises(ValueError):
            s.update_last_price(float("inf"))

    def test_update_last_price_rejects_string(self) -> None:
        s = self._make_state()
        with pytest.raises(TypeError):
            s.update_last_price("fifty")  # type: ignore[arg-type]

    def test_update_last_price_rejects_bool(self) -> None:
        s = self._make_state()
        with pytest.raises(TypeError):
            s.update_last_price(True)  # type: ignore[arg-type]

    def test_from_dict_rejects_string_last_price(self) -> None:
        legacy = {
            "pair": "X", "upper_bound": 110.0, "lower_bound": 90.0,
            "grid_levels": _linear_levels(90.0, 110.0, 5), "filled_levels": [],
            "initial_entry_price": 100.0, "last_price": "fifty",
            "created_at": None, "last_updated": None,
        }
        with pytest.raises(TypeError):
            GridState.from_dict(legacy)

    def test_from_dict_rejects_nan_last_price(self) -> None:
        legacy = {
            "pair": "X", "upper_bound": 110.0, "lower_bound": 90.0,
            "grid_levels": _linear_levels(90.0, 110.0, 5), "filled_levels": [],
            "initial_entry_price": 100.0, "last_price": float("nan"),
            "created_at": None, "last_updated": None,
        }
        with pytest.raises(ValueError):
            GridState.from_dict(legacy)

    def test_legacy_dict_migrates_last_price(self) -> None:
        legacy = {
            "pair": "ETH/USDT",
            "upper_bound": 4000.0, "lower_bound": 3000.0,
            "grid_count": 5,  # legacy field; should be silently discarded
            "grid_levels": _linear_levels(3000.0, 4000.0, 5),
            "filled_levels": [],
            "initial_entry_price": 3500.0,
            "created_at": None, "last_updated": None,
        }
        restored = GridState.from_dict(legacy)
        assert restored.last_price == 3500.0   # fallback from initial_entry_price
        assert restored.is_active()
        assert restored.grid_count == 5


# ===========================================================================
# GridTradingModule behavioral tests
# ===========================================================================

def _make_initialized_module(
    grid_count: int = 5, initial_stake: float = 500.0
) -> GridTradingModule:
    """Return a GridTradingModule with config seeded into shared_state."""
    from algo_system.orchestrator.shared_state import SharedState
    import os, tempfile
    ss = SharedState(
        persistence_path=os.path.join(tempfile.gettempdir(), f"lats_test_grid_module_{os.getpid()}.json")
    )
    ss.set("grid_trading_v1", "config", {
        "upper_bound_pct": 0.05,
        "lower_bound_pct": 0.05,
        "grid_count": grid_count,
        "initial_stake": initial_stake,
    })
    ctx = make_module_context(shared_state=ss)
    mod = GridTradingModule()
    mod.initialize(ctx)
    return mod


def _seed_grid(
    mod: GridTradingModule, pair: str = "BTC/USDT"
) -> tuple[GridState, "object"]:
    """Trigger grid construction via generate_entry_signal and return the GridState + ctx."""
    df = make_ohlcv_df(100, start_price=50_000.0)
    ctx = make_module_context(pair=pair)
    sig = mod.generate_entry_signal(df, {"pair": pair}, ctx)
    assert pair in mod._grid_states, "generate_entry_signal must build the grid"
    return mod._grid_states[pair], ctx


class TestGridTradingModuleEntrySignal:
    def test_first_call_builds_grid(self) -> None:
        mod = _make_initialized_module()
        df = make_ohlcv_df(100)
        ctx = make_module_context(pair="BTC/USDT")
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert "BTC/USDT" in mod._grid_states

    def test_entry_signal_has_correct_tag(self) -> None:
        mod = _make_initialized_module()
        df = make_ohlcv_df(100)
        ctx = make_module_context(pair="BTC/USDT")
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        if sig.enter_long:
            assert sig.entry_tag is not None and sig.entry_tag.startswith("grid_trading_v1:")

    def test_existing_grid_returns_empty_signal(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        _seed_grid(mod, pair=pair)
        # Second call should NOT emit entry — grid already exists
        df = make_ohlcv_df(100)
        ctx = make_module_context(pair=pair)
        sig = mod.generate_entry_signal(df, {"pair": pair}, ctx)
        assert sig.enter_long is not True


class TestGridTradingModuleAdjustPosition:
    def test_no_grid_returns_none(self) -> None:
        mod = _make_initialized_module()
        trade = MockTrade(pair="BTC/USDT", open_rate=50_000.0)
        ctx = make_module_context(pair="BTC/USDT")
        # No grid yet — adjust_position is a no-op
        result = mod.adjust_position(trade, None, 50_000.0, 0.0, None, 1000.0, ctx)
        assert result is None
        assert "BTC/USDT" not in mod._grid_states

    def test_downward_crossing_returns_positive_stake(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        # Pick an interior level (not the bottom) so current_rate just below it stays in_range
        assert len(state.grid_levels) >= 3, f"test needs >=3 levels, got {state.grid_levels}"
        target = state.grid_levels[1]  # second from bottom — interior, in range

        state.update_last_price(target + abs(target) * 0.0001 + 0.01)
        result = mod.adjust_position(trade, None, target - abs(target) * 0.0001 - 0.01, 0.0, None, 100_000.0, ctx)
        assert result is not None and result > 0.0, f"expected positive buy stake, got {result}"

    def test_upward_crossing_filled_level_returns_negative_stake(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        # Pick an interior level for the sell (not the top)
        assert len(state.grid_levels) >= 3
        sell_level = state.grid_levels[1]
        state.mark_filled(sell_level)

        state.update_last_price(sell_level - abs(sell_level) * 0.0001 - 0.01)
        result = mod.adjust_position(
            trade, None, sell_level + abs(sell_level) * 0.0001 + 0.01,
            0.0, None, 100_000.0, ctx,
        )
        assert result is not None and result < 0.0, f"expected negative sell stake, got {result}"

    def test_out_of_range_price_returns_none(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        out_of_range = state.upper_bound * 2.0
        state.update_last_price(out_of_range - 1.0)
        result = mod.adjust_position(trade, None, out_of_range, 0.0, None, 100_000.0, ctx)
        assert result is None

    def test_stake_exceeds_max_returns_none(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=10_000.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        # Use an interior level to keep current_rate inside grid bounds
        assert len(state.grid_levels) >= 3
        target = state.grid_levels[1]
        state.update_last_price(target + abs(target) * 0.0001 + 0.01)
        # stake_per_level = 10000/5 = 2000; max_stake=10 → blocked
        result = mod.adjust_position(
            trade, None, target - abs(target) * 0.0001 - 0.01,
            0.0, min_stake=None, max_stake=10.0, ctx=ctx,
        )
        assert result is None


class TestGridTradingModulePrevRateTracking:
    """BUG FIX (regression test): prev_rate must come from state.last_price, not trade.open_rate."""

    def test_second_candle_uses_state_last_price(self) -> None:
        """After price drifts to between two levels, the next candle that crosses
        below must be detected — using state.last_price as prev_rate, NOT
        the immutable trade.open_rate.
        """
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        # Use two interior levels we know exist
        assert len(state.grid_levels) >= 3
        target = state.grid_levels[1]   # the level we'll cross
        above = state.grid_levels[2]    # the next level up
        between = (target + above) / 2.0  # last_price sits between them
        state.update_last_price(between)
        # prev_rate should be `between`; cross target downward
        result = mod.adjust_position(
            trade, None, target - abs(target) * 0.0001 - 0.01, 0.0, None, 100_000.0, ctx
        )
        assert result is not None and result > 0.0
        # confirm last_price actually updated to current_rate (not stuck at `between`)
        assert state.last_price < between

    def test_no_double_buy_same_level_consecutive_candles(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        assert len(state.grid_levels) >= 3
        target = state.grid_levels[1]
        state.update_last_price(target + abs(target) * 0.0001 + 0.01)

        result1 = mod.adjust_position(
            trade, None, target - abs(target) * 0.0001 - 0.01, 0.0, None, 100_000.0, ctx
        )
        assert result1 is not None and result1 > 0.0
        assert state.is_level_filled(target)

        # Next call at the same below-target price — no new crossing, level already filled
        result2 = mod.adjust_position(
            trade, None, target - abs(target) * 0.0002 - 0.02, 0.0, None, 100_000.0, ctx
        )
        assert result2 is None


class TestGridTradingModuleOrderFilledSide:
    """BUG FIX: on_order_filled must mark_unfilled on sell, mark_filled on buy."""

    def _make_order(self, price: float, side: str) -> object:
        class _Order: ...
        o = _Order()
        o.price = price        # type: ignore[attr-defined]
        o.side = side          # type: ignore[attr-defined]
        return o

    def test_sell_fill_marks_level_unfilled(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        level = state.grid_levels[len(state.grid_levels) // 2]
        state.mark_filled(level)
        assert state.is_level_filled(level)

        sell_order = self._make_order(price=level, side="sell")
        mod.on_order_filled(pair, trade, sell_order, None, ctx)
        assert not state.is_level_filled(level)

    def test_buy_fill_marks_level_filled(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        state, ctx = _seed_grid(mod, pair=pair)
        trade = MockTrade(pair=pair, open_rate=state.initial_entry_price or 50_000.0)

        level = state.grid_levels[1]
        assert not state.is_level_filled(level)

        buy_order = self._make_order(price=level, side="buy")
        mod.on_order_filled(pair, trade, buy_order, None, ctx)
        assert state.is_level_filled(level)


class TestGridTradingModuleResetState:
    def test_reset_clears_grid_and_config(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        _seed_grid(mod, pair=pair)
        assert pair in mod._grid_states
        mod._configs[pair] = GridConfig()
        mod.reset_module_state(pair)
        assert pair not in mod._grid_states
        assert pair not in mod._configs
        assert pair not in mod._defer_counts

    def test_no_legacy_last_prices_attribute(self) -> None:
        mod = _make_initialized_module()
        # _last_prices was removed — last_price now lives on GridState
        assert not hasattr(mod, "_last_prices")

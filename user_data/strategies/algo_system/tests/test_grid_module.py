"""
tests/test_grid_module.py
Unit tests for GridCalculator, GridState, and GridTradingModule.
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

from algo_system.modules.grid_trading.grid_calculator import GridCalculator
from algo_system.modules.grid_trading.grid_state import GridState  # type: ignore[import]
from algo_system.modules.grid_trading.grid_trading_module import GridTradingModule  # type: ignore[import]

from test_helpers import MockTrade, make_module_context, make_ohlcv_df


# ===========================================================================
# GridCalculator tests
# ===========================================================================

class TestGridCalculatorLevels:
    def test_calculate_levels_count(self) -> None:
        levels = GridCalculator.calculate_levels(100.0, 200.0, 5)
        assert len(levels) == 5

    def test_calculate_levels_endpoints(self) -> None:
        levels = GridCalculator.calculate_levels(100.0, 200.0, 5)
        assert levels[0] == pytest.approx(100.0)
        assert levels[-1] == pytest.approx(200.0)

    def test_calculate_levels_even_spacing(self) -> None:
        levels = GridCalculator.calculate_levels(10.0, 110.0, 6)
        spacings = [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]
        assert all(abs(s - spacings[0]) < 1e-9 for s in spacings)

    def test_validate_grid_lower_geq_upper(self) -> None:
        with pytest.raises(ValueError):
            GridCalculator.validate_grid(200.0, 100.0, 5)

    def test_validate_grid_count_too_small(self) -> None:
        with pytest.raises(ValueError):
            GridCalculator.validate_grid(100.0, 200.0, 1)

    def test_validate_grid_non_positive_bounds(self) -> None:
        with pytest.raises(ValueError):
            GridCalculator.validate_grid(-10.0, 100.0, 5)


class TestGridCalculatorCrossedDown:
    def _levels(self) -> list:
        return GridCalculator.calculate_levels(90.0, 110.0, 5)
        # levels: [90, 95, 100, 105, 110]

    def test_no_crossing_when_price_flat(self) -> None:
        levels = self._levels()
        assert GridCalculator.get_crossed_levels_down(100.0, 100.0, levels) == []

    def test_no_crossing_when_price_rises(self) -> None:
        levels = self._levels()
        assert GridCalculator.get_crossed_levels_down(95.0, 100.0, levels) == []

    def test_single_level_crossed(self) -> None:
        levels = self._levels()
        crossed = GridCalculator.get_crossed_levels_down(101.0, 99.0, levels)
        assert crossed == [100.0]

    def test_multiple_levels_crossed_sorted_desc(self) -> None:
        levels = self._levels()
        # Price falls from 106 to 94 — crosses 105 and 100 and 95
        crossed = GridCalculator.get_crossed_levels_down(106.0, 94.0, levels)
        assert crossed == [105.0, 100.0, 95.0]
        # Sorted descending — closest first
        assert crossed == sorted(crossed, reverse=True)

    def test_boundary_exact_to_price(self) -> None:
        levels = self._levels()
        # to_price == level exactly should include that level (to_price <= level)
        crossed = GridCalculator.get_crossed_levels_down(101.0, 100.0, levels)
        assert 100.0 in crossed

    def test_boundary_exact_from_price(self) -> None:
        levels = self._levels()
        # level == from_price should NOT be included (level < from_price)
        crossed = GridCalculator.get_crossed_levels_down(100.0, 95.0, levels)
        assert 100.0 not in crossed


class TestGridCalculatorCrossedUp:
    def _levels(self) -> list:
        return GridCalculator.calculate_levels(90.0, 110.0, 5)
        # [90, 95, 100, 105, 110]

    def test_no_crossing_when_price_flat(self) -> None:
        levels = self._levels()
        assert GridCalculator.get_crossed_levels_up(100.0, 100.0, levels, {100.0}) == []

    def test_no_crossing_when_price_falls(self) -> None:
        levels = self._levels()
        assert GridCalculator.get_crossed_levels_up(100.0, 95.0, levels, {100.0}) == []

    def test_no_crossing_unfilled_level(self) -> None:
        levels = self._levels()
        # Level 100 not filled → no trigger even if crossed
        assert GridCalculator.get_crossed_levels_up(99.0, 101.0, levels, set()) == []

    def test_single_filled_level_crossed(self) -> None:
        levels = self._levels()
        crossed = GridCalculator.get_crossed_levels_up(99.0, 101.0, levels, {100.0})
        assert crossed == [100.0]

    def test_multiple_filled_levels_crossed_sorted_asc(self) -> None:
        levels = self._levels()
        filled = {95.0, 100.0, 105.0}
        crossed = GridCalculator.get_crossed_levels_up(94.0, 106.0, levels, filled)
        assert crossed == [95.0, 100.0, 105.0]
        assert crossed == sorted(crossed)

    def test_only_filled_subset_returned(self) -> None:
        levels = self._levels()
        # 95 and 105 filled; 100 not filled
        filled = {95.0, 105.0}
        crossed = GridCalculator.get_crossed_levels_up(94.0, 106.0, levels, filled)
        assert 100.0 not in crossed
        assert 95.0 in crossed and 105.0 in crossed


# ===========================================================================
# GridState tests
# ===========================================================================

class TestGridState:
    def _make_state(self) -> GridState:
        levels = GridCalculator.calculate_levels(45_000.0, 55_000.0, 5)
        return GridState(
            pair="BTC/USDT",
            upper_bound=55_000.0,
            lower_bound=45_000.0,
            grid_count=5,
            grid_levels=levels,
            filled_levels=set(),
        )

    def test_initial_state_empty_filled(self) -> None:
        state = self._make_state()
        assert len(state.filled_levels) == 0

    def test_mark_filled(self) -> None:
        state = self._make_state()
        level = state.grid_levels[2]
        state.mark_filled(level)
        assert state.is_level_filled(level)

    def test_mark_unfilled(self) -> None:
        state = self._make_state()
        level = state.grid_levels[1]
        state.mark_filled(level)
        state.mark_unfilled(level)
        assert not state.is_level_filled(level)

    def test_mark_unfilled_noop_on_empty(self) -> None:
        state = self._make_state()
        # Should not raise even if level not filled
        state.mark_unfilled(state.grid_levels[0])

    def test_is_in_range(self) -> None:
        state = self._make_state()
        assert state.is_in_range(50_000.0)
        assert not state.is_in_range(44_000.0)
        assert not state.is_in_range(56_000.0)

    def test_get_unfilled_levels(self) -> None:
        state = self._make_state()
        state.mark_filled(state.grid_levels[0])
        unfilled = state.get_unfilled_levels()
        assert state.grid_levels[0] not in unfilled
        assert len(unfilled) == state.grid_count - 1

    def test_validation_upper_leq_lower_raises(self) -> None:
        levels = GridCalculator.calculate_levels(90.0, 110.0, 5)
        with pytest.raises(ValueError):
            GridState(
                pair="X",
                upper_bound=80.0,
                lower_bound=90.0,
                grid_count=5,
                grid_levels=levels,
                filled_levels=set(),
            )

    def test_serialisation_roundtrip(self) -> None:
        state = self._make_state()
        state.mark_filled(state.grid_levels[1])
        restored = GridState.from_dict(state.to_dict())
        assert restored.pair == state.pair
        assert restored.grid_levels == state.grid_levels
        assert restored.filled_levels == state.filled_levels


# ===========================================================================
# GridTradingModule tests
# ===========================================================================

def _make_initialized_module(grid_count: int = 5, initial_stake: float = 500.0) -> GridTradingModule:
    """Return a GridTradingModule with config seeded into shared_state."""
    from algo_system.orchestrator.shared_state import SharedState
    import os, tempfile
    ss = SharedState(
        persistence_path=os.path.join(tempfile.gettempdir(), "lats_test_grid_module.json")
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


class TestGridTradingModuleAdjustPosition:
    def test_first_call_initializes_grid(self) -> None:
        mod = _make_initialized_module()
        trade = MockTrade(pair="BTC/USDT", open_rate=50_000.0)
        ctx = make_module_context()
        # First call — no grid yet; should return None (no crossing on first candle)
        result = mod.adjust_position(trade, None, 50_000.0, 0.0, None, 1000.0, ctx)
        assert "BTC/USDT" in mod._grid_states
        # No crossing on first candle (prev_rate == open_rate == current_rate)
        assert result is None

    def test_downward_crossing_returns_positive_stake(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        # Seed the grid first
        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # Price must fall through a level below center
        level_below = state.grid_levels[1]  # second level from bottom
        # Set open_rate above that level and current_rate below it
        trade.open_rate = level_below + 100.0
        result = mod.adjust_position(trade, None, level_below - 1.0, 0.0, None, 1000.0, ctx)
        assert result is not None
        assert result > 0.0, f"Expected positive buy stake, got {result}"

    def test_upward_crossing_filled_level_returns_negative_stake(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        # Initialize grid
        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # Manually mark a level as filled
        sell_level = state.grid_levels[2]
        state.mark_filled(sell_level)

        # Price rises through filled level
        trade.open_rate = sell_level - 1.0
        result = mod.adjust_position(trade, None, sell_level + 1.0, 0.0, None, 1000.0, ctx)
        assert result is not None
        assert result < 0.0, f"Expected negative sell stake, got {result}"

    def test_out_of_range_price_returns_none(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        # Initialize grid at 50k center
        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # Price far outside grid bounds
        out_of_range_price = state.upper_bound * 2.0
        trade.open_rate = out_of_range_price - 1.0
        result = mod.adjust_position(trade, None, out_of_range_price, 0.0, None, 1000.0, ctx)
        assert result is None

    def test_min_stake_respected(self) -> None:
        mod = _make_initialized_module(grid_count=5, initial_stake=100.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # Stake per level = 100/5 = 20; set min_stake = 50
        level_below = state.grid_levels[1]
        trade.open_rate = level_below + 100.0
        result = mod.adjust_position(
            trade, None, level_below - 1.0, 0.0, min_stake=50.0, max_stake=1000.0, ctx=ctx
        )
        assert result is not None
        assert result >= 50.0

    def test_stake_exceeds_max_returns_none(self) -> None:
        mod = _make_initialized_module(grid_count=2, initial_stake=10_000.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # stake_per_level = 10000/2 = 5000; max_stake = 10 → blocked
        level_below = state.grid_levels[0]
        trade.open_rate = level_below + 100.0
        result = mod.adjust_position(
            trade, None, level_below - 1.0, 0.0, min_stake=None, max_stake=10.0, ctx=ctx
        )
        assert result is None


class TestGridTradingModuleEntrySignal:
    def test_no_grid_active_returns_entry(self) -> None:
        mod = _make_initialized_module()
        df = make_ohlcv_df(100)
        ctx = make_module_context()
        sig = mod.generate_entry_signal(df, {"pair": "BTC/USDT"}, ctx)
        assert sig.enter_long is True
        assert sig.entry_tag is not None
        assert sig.entry_tag.startswith("grid_trading_v1:")

    def test_active_grid_returns_empty_signal(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)
        trade = MockTrade(pair=pair)

        # Activate grid
        mod.adjust_position(trade, None, 50_000.0, 0.0, None, 1000.0, ctx)

        df = make_ohlcv_df(100)
        sig = mod.generate_entry_signal(df, {"pair": pair}, ctx)
        assert sig.enter_long is not True  # no new entry when grid active


# ===========================================================================
# Regression tests for bugs found by adversarial bug hunt
# ===========================================================================


class TestGridTradingModulePrevRateTracking:
    """BUG FIX: prev_rate must track last seen price, not trade.open_rate."""

    def test_second_candle_uses_last_seen_price_not_open_rate(self) -> None:
        """
        trade.open_rate is immutable after entry.  If prev_rate reads it each
        candle, any level below open_rate can never be crossed after the first
        candle because from_price == to_price == open_rate for downward checks.

        This test verifies that after price drifts to just above a level (without
        crossing it), the next candle crossing that level is still detected correctly
        using the last-seen price, not the fixed trade.open_rate.
        """
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        # Candle 1: seed the grid; last_price records center
        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # between_price is ABOVE levels[1] but below center — no crossing yet
        # (levels[1] < between_price < levels[2]==center)
        target_level = state.grid_levels[1]
        between_price = (target_level + state.grid_levels[2]) / 2.0

        # Candle 2: price at between_price — drifts down but not past levels[1]
        # trade.open_rate stays at center (50k) — with old code this would be prev_rate
        mod.adjust_position(trade, None, between_price, 0.0, None, 1000.0, ctx)
        # Verify last_price was updated, NOT trade.open_rate
        assert mod._last_prices[pair] == pytest.approx(between_price)

        # Candle 3: price crosses levels[1] downward (from between_price → below)
        # prev_rate should be between_price, not the original center
        below_level = target_level - 1.0
        result = mod.adjust_position(trade, None, below_level, 0.0, None, 1000.0, ctx)
        assert result is not None, (
            "Downward crossing should be detected using last seen price, not trade.open_rate"
        )
        assert result > 0.0

    def test_no_double_buy_same_level_consecutive_candles(self) -> None:
        """
        After triggering a buy on a level, the level is optimistically marked filled.
        The next candle at the same price should NOT trigger another buy.
        """
        mod = _make_initialized_module(grid_count=5, initial_stake=500.0)
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)

        center = 50_000.0
        trade = MockTrade(pair=pair, open_rate=center)

        # Seed grid
        mod.adjust_position(trade, None, center, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        level_below = state.grid_levels[1]
        above_level = level_below + 100.0

        # Candle 1: explicitly set last_price to above_level
        # (simulate price was above the level on last candle)
        mod._last_prices[pair] = above_level

        # Candle 2: price drops below the level → BUY triggered, level marked filled
        result1 = mod.adjust_position(trade, None, level_below - 1.0, 0.0, None, 1000.0, ctx)
        assert result1 is not None and result1 > 0.0, "first crossing should trigger buy"
        assert state.is_level_filled(level_below), "level should be optimistically marked filled"

        # Candle 3: price still below level (or same) — no new crossing, level is filled
        result2 = mod.adjust_position(trade, None, level_below - 1.0, 0.0, None, 1000.0, ctx)
        assert result2 is None, "second candle at same price must not duplicate the buy"


class TestGridTradingModuleOrderFilledSide:
    """BUG FIX: on_order_filled must mark_unfilled on sell, mark_filled on buy."""

    def _make_order(self, price: float, side: str) -> object:
        class _Order:
            pass
        o = _Order()
        o.price = price  # type: ignore[attr-defined]
        o.side = side    # type: ignore[attr-defined]
        return o

    def test_sell_fill_marks_level_unfilled(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)
        trade = MockTrade(pair=pair, open_rate=50_000.0)

        mod.adjust_position(trade, None, 50_000.0, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        # Manually mark a level as filled
        level = state.grid_levels[2]
        state.mark_filled(level)
        assert state.is_level_filled(level)

        # Simulate a sell fill at that level
        sell_order = self._make_order(price=level, side="sell")
        mod.on_order_filled(pair, trade, sell_order, None, ctx)

        assert not state.is_level_filled(level), (
            "sell fill must mark the level unfilled for future rebuying"
        )

    def test_buy_fill_marks_level_filled(self) -> None:
        mod = _make_initialized_module()
        pair = "BTC/USDT"
        ctx = make_module_context(pair=pair)
        trade = MockTrade(pair=pair, open_rate=50_000.0)

        mod.adjust_position(trade, None, 50_000.0, 0.0, None, 1000.0, ctx)
        state = mod._grid_states[pair]

        level = state.grid_levels[1]
        assert not state.is_level_filled(level)

        buy_order = self._make_order(price=level, side="buy")
        mod.on_order_filled(pair, trade, buy_order, None, ctx)

        assert state.is_level_filled(level), "buy fill must mark the level filled"

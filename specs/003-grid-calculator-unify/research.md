# Research: Grid Calculator Unification

**Feature**: 003-grid-calculator-unify
**Phase**: 0 
 Resolve unknowns before design
**Date**: 2026-03-22

---

## 1. Full Import Surface (All Files That Reference V1 or V2)

### Files to update during implementation

| File | Current Import | New Import |
|------|---------------|------------|
| `modules/grid_trading/grid_trading_module.py` | `grid_calculator.GridCalculator` + `grid_calculator_v2.GridConfigV2, generate_grid_levels, calculate_midprice` | `config.grid_config.GridConfig` + `modules/grid_trading/grid_calculator.GridCalculator` (unified) |
| `orchestrator/optuna_grid_optimizer.py` | `from ..modules.grid_trading.grid_calculator_v2 import GridConfigV2` + `VALID_INDICATORS` | `from ..config.grid_config import GridConfig, VALID_INDICATORS` |
| `orchestrator/optuna_grid_optimizer.py` (line 788) | `from ..modules.grid_trading.grid_calculator_v2 import VALID_INDICATORS` | `from ..config.grid_config import VALID_INDICATORS` |
| `tests/test_grid_module.py` | `from algo_system.modules.grid_trading.grid_calculator import GridCalculator` | same path 
 new unified `GridCalculator` replaces in-place |
| `tests/test_integration_optimizer_to_module.py` | `from algo_system.modules.grid_trading.grid_calculator_v2 import GridConfigV2` | `from algo_system.config.grid_config import GridConfig` |
| `tests/test_optuna_optimizer.py` | `from algo_system.modules.grid_trading.grid_calculator_v2 import GridConfigV2` | `from algo_system.config.grid_config import GridConfig` |

### Files that reference by name only (no import change needed)
- `morning_run.py` line 490: references GridConfigV2 param *names* as string comments only 
 no import, no change needed.
- `research/gym_origin/grid_trading_gym_v2.py`: research artifact, out of scope.

---

## 2. Functions to Migrate from `grid_calculator_v2.py` 
 `GridCalculator`

The unified `GridCalculator` absorbs all functions from `grid_calculator_v2.py` as instance methods (not module-level functions):

| V2 Function (module-level) | Becomes (GridCalculator method) |
|----------------------------|---------------------------------|
| `calculate_midprice(df, config)` | `GridCalculator.calculate_midprice(df, config)` |
| `generate_grid_levels(midprice, grid_distance, grid_range, auto_adjust, log_scale, df)` | `GridCalculator.generate_levels(config, midprice, df=None)` |
| `auto_set_grid_parameters(df, config)` | `GridCalculator._auto_set_grid_parameters(df, config)` (private helper) |
| `auto_set_period(df, config)` | `GridCalculator._auto_set_period(df, config)` (private helper) |
| `calculate_allocation(config, trade_value, ...)` | `GridCalculator.calculate_allocation(config, trade_value, ...)` |
| `validate_grid(lower, upper, count)` | Absorbed into `GridConfig.validate()` |
| `calculate_levels(lower, upper, count)` | `GridCalculator.generate_levels()` (linear fallback path within) |
| `get_crossed_levels_down(from, to, levels)` | `GridCalculator.levels_crossed_down(from_price, to_price, levels)` (rename for clarity) |
| `get_crossed_levels_up(from, to, levels, filled)` | `GridCalculator.levels_crossed_up(from_price, to_price, levels, filled)` (rename for clarity) |
| `get_nearest_level(price, levels)` | `GridCalculator.nearest_level(price, levels)` (rename for clarity) |
| `get_level_below(price, levels)` | `GridCalculator.level_below(price, levels)` |
| `get_level_above(price, levels)` | `GridCalculator.level_above(price, levels)` |

**Decision**: Rename methods to drop the `get_` prefix. Shorter, more Pythonic. Tests will need to update call sites.

---

## 3. Constants: Where They Live After Refactor

`VALID_METHODS`, `VALID_ALLOCATION_STRATEGIES`, `VALID_INDICATORS` currently live in `grid_calculator_v2.py`. After refactor:

**Decision**: Move to `config/grid_config.py` alongside `GridConfig`.

**Rationale**: They are validation constants for config fields, not calculator internals. Any consumer validating a config (orchestrator, tests, optimizer) should import them from config, not from the calculator.

---

## 4. `GridConfigV2` 
 `GridConfig` Rename Strategy

**Decision**: Rename the dataclass to `GridConfig`. Drop all version suffixes.

**Migration steps**:
1. New file: `config/grid_config.py` 
 contains `GridConfig` (renamed from `GridConfigV2`) + constants.
2. Old file: `grid_calculator_v2.py` 
 deleted after all imports updated.
3. Old file: `grid_calculator.py` 
 deleted after methods absorbed into unified `GridCalculator`.

**Backwards compatibility**: No public API commitment exists for these classes outside the algo_system package. Internal refactor only.

---

## 5. Grid Initialization Lifecycle 
 Where It Happens

**Current problem**: `_initialize_grid` is called from `adjust_position()` which has no DataFrame. This blocks `log_scale` and `auto_adjust` features (both need candle data).

**Decision**: Move grid initialization into `generate_entry_signal()`.

**Flow after refactor**:
```
generate_entry_signal(df, metadata, ctx):
    if pair not in _grid_states:
        config = _resolve_config(pair, ctx)          # load from SharedState
        midprice = calculator.calculate_midprice(df, config)
        levels = calculator.generate_levels(config, midprice, df=df)  # df available!
        _grid_states[pair] = GridState(...)
    ...check entry conditions...

adjust_position(...):
    if pair not in _grid_states:
        return None  # grid not yet initialized 
 do nothing
    state = _grid_states[pair]
    ...crossing detection on existing state...
```

**Risk**: If `adjust_position` fires before `generate_entry_signal` on a restored trade, no grid is found. Mitigation: `on_bot_start` restores persisted states 
 so a restored trade already has a `GridState`.

---

## 6. `_last_prices` vs `GridState.last_price`

**Current problem**: `_last_prices` is an in-memory dict on `GridTradingModule`, not persisted. After restart, prev_rate defaults to `current_rate` (zero crossings on first candle).

**Decision**: Add `last_price: Optional[float]` field to `GridState`. Remove `_last_prices` dict from `GridTradingModule`.

**Serialization**: `last_price` included in `to_dict()` / `from_dict()`. Legacy states missing `last_price` default to `initial_entry_price`.

---

## 7. `is_active()` Semantics

**Current problem**: `is_active()` returns `self.initial_entry_price is not None`, which is always `True` for any constructed `GridState` (initial_entry_price is always set at construction).

**Decision**: `is_active()` returns `True` when `last_price is not None` (i.e., the grid has participated in at least one candle cycle). A freshly constructed-but-not-yet-cycled state returns `False`.

---

## 8. `GridState` Validation 
 V2 Level Count

**Current problem**: `__post_init__` enforces `len(grid_levels) == grid_count`. In V2, `grid_count` is computed post-hoc from `len(levels)`. The validation is satisfied only because `_initialize_grid` sets `count = len(levels)`.

**Decision after refactor**: `GridState.grid_count` is derived as `len(grid_levels)` 
 the constructor does not accept it as a separate parameter. The `__post_init__` validation for this check is removed. `grid_count` becomes a property: `@property def grid_count(self) -> int: return len(self.grid_levels)`.

---

## 9. `GridConfig.validate()` 
 Pre-Construction Validation

`GridConfig.__post_init__` already validates `method` and `allocation_strategy`. We add a `validate()` method that also checks:
- `grid_distance > 0`
- `grid_range > grid_distance` (can produce 
 2 levels)
- Degenerate config detection (would produce 1 level or fewer)

This replaces `GridCalculator.validate_grid()` (absorbed).

---

## 10. Portability Boundary

**Decision**: Three files become freqtrade-free:

| File | Freqtrade dependency |
|------|---------------------|
| `config/grid_config.py` | None 
 pure dataclass |
| `modules/grid_trading/grid_calculator.py` (unified) | None 
 pandas/numpy only (optional: talib guarded) |
| `modules/grid_trading/grid_state.py` | None 
 stdlib only |
| `modules/grid_trading/grid_trading_module.py` | Yes 
 IAlgoModule, ModuleContext |

`GridTradingModule` is the only freqtrade adapter. Its freqtrade imports are NOT guarded with `TYPE_CHECKING` 
 it is explicitly a freqtrade component. The boundary is documented in the class docstring.

---

## 11. Test File Update Strategy

Tests must not be broken 
 only their import paths and method names change:

| Test | Change |
|------|--------|
| `test_grid_module.py` | `GridCalculator` method names (`get_crossed_levels_down` 
 `levels_crossed_down`, etc.) |
| `test_integration_optimizer_to_module.py` | `GridConfigV2` 
 `GridConfig`, import path 
 `config.grid_config` |
| `test_optuna_optimizer.py` | `GridConfigV2` 
 `GridConfig`, import path 
 `config.grid_config` |

**No test assertions change** 
 only call sites and import paths. All existing behaviour is preserved.

---

## Summary of Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Single `GridCalculator` replaces V1+V2 | Eliminates split-brain, dead-code V2 features |
| 2 | `GridConfig` replaces `GridConfigV2`, in `config/` layer | Breaks upward coupling, enables portability |
| 3 | Constants move to `config/grid_config.py` | Validation constants belong with config, not calculator |
| 4 | Grid init in `generate_entry_signal` | Only place where df is available |
| 5 | `last_price` in `GridState` | Survives restarts, eliminates `_last_prices` dict |
| 6 | `is_active()` based on `last_price is not None` | Meaningful semantics (has cycled vs. just constructed) |
| 7 | `grid_count` becomes property of `GridState` | Removes V1/V2 semantic mismatch |
| 8 | `GridConfig.validate()` for degenerate detection | Early error at config time, not trading time |
| 9 | V1 `grid_calculator.py` + V2 `grid_calculator_v2.py` both deleted | No legacy code survives |
| 10 | Freqtrade boundary isolated to `GridTradingModule` only | Core is portable |

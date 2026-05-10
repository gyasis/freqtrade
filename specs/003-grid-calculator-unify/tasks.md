# Tasks: Grid Calculator Unification & Module Portability

**Input**: Design documents from `/specs/003-grid-calculator-unify/`
**Prerequisites**: plan.md 
, spec.md 
, research.md 
, data-model.md 
, contracts/ 


**Organization**: Tasks follow the 5 user stories from spec.md (US1
US5), mapped to the 6 implementation phases in plan.md. US2 (config contract) precedes US1 (calculator) because `GridCalculator` imports `GridConfig`.

---

## Phase 1: Setup

**Purpose**: Confirm layout and verify no blocking issues before touching files.

- [ ] T001 Verify existing file layout matches plan.md expectations 
 confirm presence of `config/`, `modules/grid_trading/`, `orchestrator/`, `tests/` directories
- [ ] T002 [P] Confirm all files to-be-modified exist: `grid_calculator.py`, `grid_calculator_v2.py`, `grid_state.py`, `grid_trading_module.py`, `optuna_grid_optimizer.py`
- [ ] T003 [P] Run existing test suite baseline: `pytest user_data/strategies/algo_system/tests/test_grid_module.py user_data/strategies/algo_system/tests/test_integration_optimizer_to_module.py user_data/strategies/algo_system/tests/test_optuna_optimizer.py -v` 
 record pass/fail as baseline

---

## Phase 2: Foundational 
 US2 (Config Contract) 


**Purpose**: Create `GridConfig` in `config/` before anything else 
 every other phase imports from here.

**
 CRITICAL**: No US1, US3, US4 work can begin until Phase 2 is complete.

**Goal (US2)**: `GridConfig` lives in `config/grid_config.py`, importable without pulling in pandas, numpy, or talib.

**Independent Test**: `python -c "from algo_system.config.grid_config import GridConfig, VALID_METHODS, VALID_INDICATORS; GridConfig().validate()"` 
 succeeds with zero indicator imports.

- [ ] T004 [US2] Create `user_data/strategies/algo_system/config/grid_config.py` 
 copy `GridConfigV2` dataclass fields verbatim from `grid_calculator_v2.py` and rename class to `GridConfig` (drop version suffix)
- [ ] T005 [P] [US2] Move constants `VALID_METHODS`, `VALID_ALLOCATION_STRATEGIES`, `VALID_INDICATORS` from `grid_calculator_v2.py` into `config/grid_config.py` (no imports of pandas/numpy/talib permitted in this file)
- [ ] T006 [US2] Add `validate()` method to `GridConfig` in `config/grid_config.py` 
 raise `ValueError` for: `grid_distance <= 0`, `grid_range <= grid_distance`, unsupported `method`, unsupported `allocation_strategy`, unknown `selected_indicators`; absorbs logic from V1 `GridCalculator.validate_grid()` and V2 `__post_init__`
- [ ] T007 [US2] Update `user_data/strategies/algo_system/config/__init__.py` 
 add `GridConfig` to exports
- [x] T013 [P] [US1] Implement `GridCalculator._auto_set_grid_parameters(df, config) -> GridConfig` in `grid_calculator.py` 
 port from V2 `auto_set_grid_parameters()` (ATR-driven grid_distance/grid_range derivation); talib guarded with `try/except ImportError`; fallback to pandas/numpy when talib absent
- [x] T014 [P] [US1] Implement `GridCalculator._auto_set_period(df, config) -> int` in `grid_calculator.py` 
 port from V2 `auto_set_period()` (HT_DCPERIOD or numpy autocorrelation fallback)
- [ ] T015 [P] [US1] Implement crossing-detection methods in `grid_calculator.py` 
 `levels_crossed_down(from_price, to_price, levels)`, `levels_crossed_up(from_price, to_price, levels, filled)`, `nearest_level(price, levels)`, `level_below(price, levels)`, `level_above(price, levels)` 
 port from V1 equivalents with renamed signatures (drop `get_` prefix)
- [x] T016 [US1] Implement `GridCalculator.calculate_allocation(config, trade_value, current_price, atr=None, available=None) -> float` in `grid_calculator.py` 
 port from V2 allocation dispatcher (`fixed_pct`, `fixed_shares`, `dynamic_grid`, `proportional`, `volatility`)
- [x] T017 [US1] Update `user_data/strategies/algo_system/modules/grid_trading/__init__.py` 
 export `GridCalculator` from updated file
 no version branching.

**Independent Test**: `GridCalculator.generate_levels(GridConfig(log_scale=True), 100.0)` returns log-spaced levels. `GridCalculator.generate_levels(GridConfig(), 100.0)` returns linear levels. `GridCalculator.levels_crossed_down(101.0, 99.0, levels)` returns correct buy triggers for both.

- [ ] T008 [US1] Rewrite `user_data/strategies/algo_system/modules/grid_trading/grid_calculator.py` 
 replace file header and add `from algo_system.config.grid_config import GridConfig`; remove all V1-only imports; ensure zero import from `grid_calculator_v2`
- [ ] T009 [US1] Implement `GridCalculator.generate_levels(config: GridConfig, midprice: float, df=None) -> List[float]` in `grid_calculator.py` 
 internal routing: if `config.auto_adjust` call `_auto_set_grid_parameters(df, config)` first; if `config.log_scale` call `_generate_log_levels()`; else call `_generate_linear_levels()`
- [ ] T010 [P] [US1] Implement `GridCalculator._generate_linear_levels(midprice, grid_distance, grid_range) -> List[float]` in `grid_calculator.py` 
 port from V1 `calculate_levels()` logic (equal spacing from lower to upper bound)
- [ ] T011 [P] [US1] Implement `GridCalculator._generate_log_levels(midprice, grid_distance, grid_range) -> List[float]` in `grid_calculator.py` 
 port from V2 `generate_logarithmic_grid()` equivalent in `grid_calculator_v2.py`
- [ ] T012 [US1] Implement `GridCalculator.calculate_midprice(df: DataFrame, config: GridConfig) -> float` in `grid_calculator.py` 
 routes to: `market_price` (last close), `moving_average` (SMA of period), `mid_high_low` ((high+low)/2 rolling), `vwap` (volume-weighted) 
 port from V2 `calculate_midprice()` function
- [ ] T013 [P] [US1] Implement `GridCalculator._auto_set_grid_parameters(df, config) -> GridConfig` in `grid_calculator.py` 
 port from V2 `auto_set_grid_parameters()` (ATR-driven grid_distance/grid_range derivation); talib guarded with `try/except ImportError`; fallback to pandas/numpy when talib absent
- [ ] T014 [P] [US1] Implement `GridCalculator._auto_set_period(df, config) -> int` in `grid_calculator.py` 
 port from V2 `auto_set_period()` (HT_DCPERIOD or numpy autocorrelation fallback)
- [ ] T015 [P] [US1] Implement crossing-detection methods in `grid_calculator.py` 
 `levels_crossed_down(from_price, to_price, levels)`, `levels_crossed_up(from_price, to_price, levels, filled)`, `nearest_level(price, levels)`, `level_below(price, levels)`, `level_above(price, levels)` 
 port from V1 equivalents with renamed signatures (drop `get_` prefix)
- [ ] T016 [US1] Implement `GridCalculator.calculate_allocation(config, trade_value, current_price, atr=None, available=None) -> float` in `grid_calculator.py` 
 port from V2 allocation dispatcher (`fixed_pct`, `fixed_shares`, `dynamic_grid`, `proportional`, `volatility`)
- [ ] T017 [US1] Update `user_data/strategies/algo_system/modules/grid_trading/__init__.py` 
 export `GridCalculator` from updated file

**Checkpoint 
 Phase 3 complete when**: `GridCalculator.generate_levels(GridConfig(log_scale=True), 100.0)` returns log-spaced levels with no fallback. `GridCalculator.generate_levels(GridConfig(auto_adjust=True), 100.0, df=mock_df)` completes without error. All crossing-detection methods work with renamed signatures.

---

## Phase 4: US4 
 GridState Last Price (Priority: P2)

**Goal**: `GridState.last_price` field persists through serialization, replacing the in-memory `_last_prices` dict. Restores correctly from legacy state missing this field.

**Independent Test**: Serialize a `GridState` with `last_price=50000.0`, restore with `from_dict()`, verify `last_price == 50000.0`. Serialize without `last_price` in dict, verify restore defaults to `initial_entry_price`.

- [ ] T018 [US4] Add `last_price: Optional[float] = None` field to `GridState` dataclass in `user_data/strategies/algo_system/modules/grid_trading/grid_state.py`
- [ ] T019 [US4] Add `update_last_price(self, price: float) -> None` method to `GridState` in `grid_state.py` 
 sets `self.last_price = price` and updates `self.last_updated`
- [ ] T020 [US4] Convert `grid_count` from stored `__init__` parameter to `@property` in `GridState` in `grid_state.py` 
 `return len(self.grid_levels)` 
 remove `grid_count` parameter from `__init__` signature
- [ ] T021 [US4] Remove `grid_count` parameter from `GridState.__post_init__` in `grid_state.py` 
 remove the `len(self.grid_levels) != self.grid_count` validation (now structurally impossible to violate)
- [ ] T022 [US4] Update `GridState.is_active()` in `grid_state.py` 
 change from `self.initial_entry_price is not None` to `self.last_price is not None`
- [ ] T023 [US4] Update `GridState.to_dict()` in `grid_state.py` 
 add `"last_price": self.last_price` key to serialized output
- [ ] T024 [US4] Update `GridState.from_dict()` in `grid_state.py` 
 read `data.get("last_price")` defaulting to `data.get("initial_entry_price")` when key absent; log migration notice when falling back

**Checkpoint 
 Phase 4 complete when**: `GridState(pair="X", upper_bound=110.0, lower_bound=90.0, grid_levels=[90,100,110], filled_levels=set(), initial_entry_price=100.0).is_active()` returns `False`. After `state.update_last_price(100.0)`, returns `True`. Round-trip serialization preserves `last_price`.

---

## Phase 5: US3 
 Grid Initialization Lifecycle (Priority: P2)

**Goal**: Grid initialization moves from `adjust_position()` to `generate_entry_signal()` where `df` is available, unblocking `log_scale` and `auto_adjust` features.

**Independent Test (depends on Phase 3 + 4)**: Create a `GridConfig(auto_adjust=True)`. Call `generate_entry_signal(df, metadata, ctx)` on a pair with no existing state. Verify `_grid_states[pair]` is populated. Call `adjust_position()` 
 verify it uses the cached state without re-initializing.

- [ ] T025 [US3] Update imports in `user_data/strategies/algo_system/modules/grid_trading/grid_trading_module.py` 
 replace `from ..grid_trading.grid_calculator import GridCalculator` (V1) + `from ..grid_trading.grid_calculator_v2 import GridConfigV2, generate_grid_levels, calculate_midprice` with: `from ...config.grid_config import GridConfig` + unified `GridCalculator` import only
- [ ] T026 [US3] Remove `_last_prices: Dict[str, float]` attribute from `GridTradingModule.__init__` in `grid_trading_module.py`
- [ ] T027 [US3] Rename `_v2_configs` 
 `_configs: Dict[str, GridConfig]` throughout `GridTradingModule` in `grid_trading_module.py`
- [ ] T028 [US3] Move grid initialization into `generate_entry_signal()` in `grid_trading_module.py` 
 when `pair not in self._grid_states`: call `_resolve_config(pair, ctx)`, call `GridCalculator.calculate_midprice(df, config)`, call `GridCalculator.generate_levels(config, midprice, df=df)`, construct `GridState` with `last_price=None`, cache in `self._grid_states[pair]`
- [ ] T029 [US3] Update `adjust_position()` in `grid_trading_module.py` 
 remove all initialization logic; if `pair not in self._grid_states` return `None`; use `state.last_price or current_rate` as `prev_rate`; replace `self._last_prices[pair] = current_rate` with `state.update_last_price(current_rate)`
- [ ] T030 [US3] Delete `_initialize_grid()` private method from `grid_trading_module.py` 
 logic is now absorbed into `generate_entry_signal()`
- [ ] T031 [US3] Rename `_resolve_v2_config()` 
 `_resolve_config()` in `grid_trading_module.py` 
 update return type to `Optional[GridConfig]`; update `GridConfigV2.from_dict(data)` 
 `GridConfig.from_dict(data)`
- [ ] T032 [US3] Consolidate lazy-loading in `grid_trading_module.py` 
 remove duplicate `if pair not in self._configs` guards from `adjust_position()`; opportunistic load only in `generate_entry_signal()` alongside initialization
- [ ] T033 [US3] Update `reset_module_state(pair)` in `grid_trading_module.py` 
 remove `self._last_prices.pop(pair, None)` call; rename `_v2_configs` pop to `_configs`

**Checkpoint 
 Phase 5 complete when**: `GridTradingModule` has zero references to `_last_prices`, `GridConfigV2`, `grid_calculator_v2`, `_initialize_grid`, `_resolve_v2_config`. `generate_entry_signal` initializes grid. `adjust_position` never initializes grid.

---

## Phase 6: US1+US2+US5 
 Delete Old Files & Update All Consumers (Priority: P3)

**Goal**: `grid_calculator_v2.py` deleted. All import paths in tests and orchestrator updated. No legacy references remain anywhere.

**Independent Test**: `grep -r "grid_calculator_v2\|GridConfigV2\|_last_prices\|_v2_configs" user_data/strategies/algo_system/` returns zero results.

- [ ] T034 [US5] Delete `user_data/strategies/algo_system/modules/grid_trading/grid_calculator_v2.py` from the repository
- [ ] T035 [P] [US5] Update `user_data/strategies/algo_system/orchestrator/optuna_grid_optimizer.py` 
 change `from ..modules.grid_trading.grid_calculator_v2 import GridConfigV2` 
 `from ..config.grid_config import GridConfig`; change inline `from ..modules.grid_trading.grid_calculator_v2 import VALID_INDICATORS` (line 788) 
 `from ..config.grid_config import VALID_INDICATORS`; rename all `GridConfigV2` 
 `GridConfig` throughout file
- [ ] T036 [P] [US5] Update `user_data/strategies/algo_system/tests/test_grid_module.py` 
 rename all method calls: `GridCalculator.calculate_levels(lower, upper, count)` 
 `GridCalculator.generate_levels(config, midprice)`; `GridCalculator.validate_grid(...)` 
 `GridConfig(...).validate()`; `get_crossed_levels_down` 
 `levels_crossed_down`; `get_crossed_levels_up` 
 `levels_crossed_up`; `get_nearest_level` 
 `nearest_level`; `get_level_below` 
 `level_below`; `get_level_above` 
 `level_above`; update import from `config.grid_config` for `GridConfig`
- [ ] T037 [P] [US5] Update `user_data/strategies/algo_system/tests/test_integration_optimizer_to_module.py` 
 replace all `GridConfigV2` 
 `GridConfig`; update import: `from algo_system.config.grid_config import GridConfig`; update test at line ~249 that asserted `log_scale=True` fell back to V1 
 now assert `log_scale=True` produces log-spaced levels with NO fallback
- [ ] T038 [P] [US5] Update `user_data/strategies/algo_system/tests/test_optuna_optimizer.py` 
 replace all `GridConfigV2` 
 `GridConfig`; update import: `from algo_system.config.grid_config import GridConfig`; rename `TestGridConfigV2Roundtrip` class 
 `TestGridConfigRoundtrip`

**Checkpoint 
 Phase 6 complete when**: `grid_calculator_v2.py` file does not exist. `grep -r "grid_calculator_v2\|GridConfigV2" user_data/` returns zero results.

---

## Phase 7: US5 
 Portability Verification & Final Cleanup (Priority: P3)

**Goal**: Confirm portable imports work, test suite is fully green, no legacy remnants.

- [ ] T039 [P] [US5] Portability check 
 verify in a subshell without freqtrade on sys.path that `GridConfig`, `GridCalculator`, `GridState` can be imported and exercised: `python -c "import sys; sys.path.insert(0, 'user_data/strategies'); from algo_system.config.grid_config import GridConfig; from algo_system.modules.grid_trading.grid_calculator import GridCalculator; from algo_system.modules.grid_trading.grid_state import GridState; cfg=GridConfig(); levels=GridCalculator.generate_levels(cfg, 100.0); print('PORTABLE OK', levels[:3])"`
- [ ] T040 [P] Scan for any remaining V1/V2 legacy references: `grep -rn "grid_calculator_v2\|GridConfigV2\|_last_prices\|_v2_configs\|_initialize_grid\|_resolve_v2_config\|get_crossed_levels\|get_nearest_level\|get_level_below\|get_level_above\|calculate_levels\b" user_data/strategies/algo_system/ --include="*.py"` 
 fix any found
- [ ] T041 Remove stale `__pycache__` for deleted module: `find user_data/strategies/algo_system/modules/grid_trading/__pycache__ -name "*grid_calculator_v2*" -delete`
- [ ] T042 Update docstrings in `grid_trading_module.py` 
 remove all references to "V1", "V2", "GridConfigV2", "fallback to v1", "_initialize_grid", "previous-rate proxy" (replace with updated `GridState.last_price` description)
- [ ] T043 Run full grid test suite: `pytest user_data/strategies/algo_system/tests/test_grid_module.py user_data/strategies/algo_system/tests/test_integration_optimizer_to_module.py user_data/strategies/algo_system/tests/test_optuna_optimizer.py -v` 
 must be 100% green
- [ ] T044 Verify `log_scale=True` test in `test_integration_optimizer_to_module.py` now PASSES (previously was asserting fallback-to-V1 behaviour 
 now asserts log-spaced levels produced correctly)

**Checkpoint 
 Phase 7 complete when**: All 44 tasks checked. Full test suite green. No legacy references. Portability check passes.

---

## Dependencies & Execution Order

### Phase Dependencies
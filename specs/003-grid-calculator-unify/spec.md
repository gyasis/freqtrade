# Feature Specification: Grid Calculator Unification & Module Portability

**Feature Branch**: `003-grid-calculator-unify`
**Created**: 2026-03-22
**Status**: Draft

---

## Overview

The grid trading module currently contains two parallel calculator implementations (V1 and V2) that create split-brain logic, silent feature degradation, and architectural coupling violations. This feature eliminates the V1/V2 distinction entirely 
 one `GridCalculator`, one `GridConfig`, no version suffixes, no fallback paths. The full capability of the current V2 implementation becomes the single baseline. Alongside this, the shared config contract is relocated to the correct layer, state persistence gaps are closed, and the module is made self-contained enough to be imported and operated outside of freqtrade.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 
 Unified Calculator With No Version Branching (Priority: P1)

A developer configuring grid trading for any pair sets a single `GridConfig` object. The system uses log-scale, VWAP midprice, auto-adjust, or linear spacing based on what is configured 
 there is no fallback path that silently strips features. All configuration paths reach the same crossing-detection logic.

**Why this priority**: The entire refactor hinges on this. Log-scale and auto-adjust features are currently dead code in live trading. This story unblocks all advanced grid configurations and removes the V1/V2 split entirely.

**Independent Test**: Configure a grid with `log_scale=True` and verify the generated levels are logarithmically spaced. Configure with `method="vwap"` and verify midprice uses volume-weighted data. Both paths must reach the same crossing-detection engine with zero branching.

**Acceptance Scenarios**:

1. **Given** a `GridConfig` with `log_scale=True` and valid candle data, **When** the grid is initialized, **Then** levels are logarithmically spaced and crossing detection operates on those levels with no fallback.
2. **Given** a `GridConfig` with `auto_adjust=True`, **When** the grid is initialized using volatility data from candles, **Then** spacing parameters are auto-set and the resulting grid is used directly.
3. **Given** any `GridConfig` (log or linear), **When** a price crosses a grid level downward, **Then** a buy signal is produced; when a filled level is crossed upward, a sell signal is produced 
 with identical code path for all config types.
4. **Given** a configuration that was previously blocked by "no DataFrame available" at `adjust_position` time, **When** grid initialization is invoked from `generate_entry_signal` (which has candle data), **Then** the full config is honoured.

---

### User Story 2 
 Single Shared Config Contract (Priority: P1)

A developer using `OptunaGridOptimizer` or any external tuning tool interacts with a single `GridConfig` type that lives in the shared config layer 
 not buried inside a calculator file. Any project-level component can import `GridConfig` without pulling in grid-specific calculation logic.

**Why this priority**: The current upward coupling (orchestrator importing from module internals) violates the dependency graph and blocks portability. Fixing this is a prerequisite for Story 5.

**Independent Test**: `from algo_system.config.grid_config import GridConfig` resolves without importing any grid calculation code. `OptunaGridOptimizer` uses this import path and has zero references to `grid_calculator_v2`.

**Acceptance Scenarios**:

1. **Given** `GridConfig` is defined in the `config/` layer, **When** `OptunaGridOptimizer` imports it, **Then** there is no import from `modules/grid_trading/`.
2. **Given** a downstream consumer that only needs the config type, **When** it imports `GridConfig`, **Then** it does not transitively import pandas, numpy, talib, or any indicator library.
3. **Given** `GridTradingModule` imports `GridConfig` from `config/`, **When** the module is instantiated, **Then** behaviour is identical to the current system.

---

### User Story 3 
 Grid Initialized Where Candle Data Is Available (Priority: P2)

Grid initialization (computing levels from midprice, ATR, VWAP, etc.) happens at signal-generation time 
 where the candle DataFrame is available. The position-adjustment hook only acts on an already-initialized grid.

**Why this priority**: This is the structural fix that makes log-scale and auto-adjust features actually reachable in a live candle cycle. Without it, Story 1 is satisfied only on paper.

**Independent Test**: Set `auto_adjust=True`. Run a simulated candle cycle. Observe that the grid is built during signal generation (with candle data), and that position adjustment on the next candle operates on that cached grid without re-building it.

**Acceptance Scenarios**:

1. **Given** no grid exists for a pair, **When** `generate_entry_signal` is called with candle data, **Then** the grid is built using that candle data and stored in module state.
2. **Given** a grid already exists for a pair, **When** `generate_entry_signal` is called again, **Then** no re-initialization occurs.
3. **Given** a grid was built in `generate_entry_signal`, **When** `adjust_position` is called on the same pair, **Then** it uses the pre-built grid without any initialization logic.

---

### User Story 4 
 Last Price Persisted Across Restarts (Priority: P2)

After the bot restarts and grid states are restored from storage, the crossing-detection engine has a valid "previous price" anchor for each pair. No crossings are silently missed or falsely fired on the first candle after restart.

**Why this priority**: A grid without a correct previous-price anchor misses buy/sell triggers on the first candle after every restart. This is a correctness bug.

**Independent Test**: Simulate a bot restart by serializing then restoring a `GridState`. Confirm that `last_price` is present in the restored state and that the first post-restart candle detects crossings correctly against it.

**Acceptance Scenarios**:

1. **Given** a grid state with `last_price=50000.0`, **When** `GridState.to_dict()` is called, **Then** `last_price` is present in the serialized output.
2. **Given** a serialized state with `last_price`, **When** `GridState.from_dict()` is called, **Then** the restored state carries the correct value.
3. **Given** a restored state with `last_price=50000.0`, **When** the next candle arrives at `49800.0`, **Then** a downward crossing is correctly detected and a buy trigger is produced.
4. **Given** a legacy serialized state with no `last_price` field, **When** it is restored, **Then** `last_price` defaults to `initial_entry_price` and a migration notice is logged.

---

### User Story 5 
 Portable Core (No Hidden Freqtrade Coupling) (Priority: P3)

The grid trading core 
 calculator, state, config 
 can be imported and used in a standalone Python environment: a backtesting notebook, a research script, or a different trading framework 
 without needing freqtrade installed.

**Why this priority**: Portability enables reuse across other projects. The freqtrade adapter layer stays, but the core components must not bleed freqtrade dependencies.

**Independent Test**: In a minimal Python environment (no freqtrade), import `GridCalculator`, `GridState`, and `GridConfig`. Instantiate a calculator, generate levels, simulate crossings. All steps must succeed without ImportError.

**Acceptance Scenarios**:

1. **Given** a Python environment without freqtrade, **When** `GridCalculator`, `GridState`, and `GridConfig` are imported, **Then** no ImportError is raised.
2. **Given** a non-freqtrade project, **When** `GridCalculator` is used to generate levels and detect crossings, **Then** the full feature set (log-scale, auto-adjust, all methods) is available.
3. **Given** a freqtrade environment, **When** `GridTradingModule` is imported, **Then** it still functions as a first-class freqtrade module using the same core components.

---

### Edge Cases

- What happens when `auto_adjust=True` but the candle DataFrame has insufficient history for volatility calculation? 
 System falls back to config-provided `grid_distance` and `grid_range`, logs a warning. No silent failure.
- What happens when `method="vwap"` but volume data is missing? 
 System falls back to `method="market_price"` and logs a warning.
- What happens when `grid_distance` produces only 1 level given the `grid_range`? 
 `GridConfig.validate()` raises `ValueError` at config-construction time, not at trading time.
- What happens when a legacy grid state (no `last_price`) is restored? 
 `last_price` defaults to `initial_entry_price` with a logged migration notice. Transparent and non-breaking.
- What happens when `OptunaGridOptimizer` provides a config for a pair not currently in the whitelist? 
 Config is stored; applied when that pair becomes active. No error at injection time.

---

## Requirements *(mandatory)*

### Functional Requirements

**Calculator Unification**

- **FR-001**: The system MUST expose a single `GridCalculator` that subsumes all capabilities currently split across `grid_calculator.py` and `grid_calculator_v2.py`. Both original files MUST be removed.
- **FR-002**: `GridCalculator` MUST support all spacing methods: linear, log-scale, and auto-adjust (volatility-driven). Method selection is driven by `GridConfig`, not code-path branching.
- **FR-003**: `GridCalculator` MUST expose a `generate_levels(config, midprice, df=None)` interface. When `auto_adjust=True` or `method` requires candle data, `df` MUST be provided; otherwise it is optional.
- **FR-004**: `GridCalculator` MUST expose crossing-detection operations (`levels_crossed_down`, `levels_crossed_up`, `nearest_level`) that operate on any level list regardless of how it was generated.
- **FR-005**: The system MUST NOT contain any code that silently falls back from an advanced config to a simpler one. Any fallback MUST be explicit, logged at WARNING level, and triggered only by a documented degraded-data condition (e.g., missing volume for VWAP).

**Config Contract**

- **FR-006**: `GridConfig` MUST be defined in `config/grid_config.py` and MUST NOT be defined inside any calculator or module file.
- **FR-007**: `GridConfig` MUST be importable without transitively importing any indicator library (talib, pandas-ta, etc.).
- **FR-008**: `GridConfig` MUST expose a `validate()` method that raises `ValueError` for degenerate configurations before any grid is constructed.
- **FR-009**: `OptunaGridOptimizer` MUST import `GridConfig` from `config/grid_config.py`. No import from `grid_calculator_v2` MUST remain.

**State & Persistence**

- **FR-010**: `GridState` MUST include a `last_price` field serialized by `to_dict()` and deserialized by `from_dict()`.
- **FR-011**: When restoring a `GridState` with no `last_price` in the serialized data, the system MUST default `last_price` to `initial_entry_price` and log a migration notice.
- **FR-012**: `GridTradingModule` MUST update `GridState.last_price` on every candle where position adjustment runs. The separate `_last_prices` dict MUST be removed.
- **FR-013**: `GridState.is_active()` MUST return `True` only when the grid is initialized AND has at least one `last_price` observation recorded (i.e., has participated in at least one candle cycle).

**Initialization Lifecycle**

- **FR-014**: Grid initialization (level generation from midprice and config) MUST occur inside `generate_entry_signal()`, which receives the candle DataFrame. Position adjustment MUST NOT perform grid initialization.
- **FR-015**: The module MUST cache the initialized `GridState` immediately after creation so that position adjustment finds it on the next call.

**Portability**

- **FR-016**: `GridCalculator`, `GridState`, and `GridConfig` MUST be importable in a Python environment where freqtrade is not installed.
- **FR-017**: `GridTradingModule` MAY depend on freqtrade internals, but MUST document this boundary explicitly.
- **FR-018**: All freqtrade-specific imports in `GridCalculator`, `GridState`, and `GridConfig` MUST be removed or guarded with `TYPE_CHECKING`.

### Key Entities

- **GridConfig**: Single source of truth for all grid parameters. Defines spacing method, distance, range, midprice method, allocation strategy, log-scale flag, auto-adjust flag. Lives in `config/` layer. Freqtrade-independent.
- **GridCalculator**: Stateless pure-math component. Accepts `GridConfig` + optional candle data and generates levels, detects crossings, computes nearest levels. No internal state. No freqtrade dependency.
- **GridState**: Per-pair mutable state. Owns `grid_levels`, `filled_levels`, `last_price`, `initial_entry_price`, bounds, timestamps. Fully serializable. No freqtrade dependency.
- **GridTradingModule**: Freqtrade adapter. Implements `IAlgoModule`. Orchestrates `GridCalculator` + `GridState` using `ModuleContext`. Freqtrade dependency is explicit and isolated to this class only.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All advanced grid configuration options (`log_scale=True`, `auto_adjust=True`, `method="vwap"`) produce correctly configured grids with zero silent fallback during a full simulated candle cycle.
- **SC-002**: After a simulated bot restart (serialize 
 first candle), crossing detection produces the correct buy/sell decisions on the first post-restart candle for 100% of test scenarios.
- **SC-003**: The entire existing test suite passes without modification to test assertions after the unification.
- **SC-004**: `GridCalculator`, `GridState`, and `GridConfig` are importable and fully exercisable in a standalone environment with no freqtrade present and zero ImportErrors.
- **SC-005**: `OptunaGridOptimizer` imports `GridConfig` exclusively from the shared config layer 
 zero references to `grid_calculator_v2` remain anywhere in the codebase.
- **SC-006**: Both `grid_calculator.py` and `grid_calculator_v2.py` are deleted from the repository with no remaining imports pointing to them.
- **SC-007**: The `_last_prices` dict is eliminated from `GridTradingModule` with `GridState.last_price` serving as the sole source of previous-price data.

---

## Assumptions

- `GridConfigV2` in `grid_calculator_v2.py` is the correct and complete parameter set. V1 parameters (`upper_bound_pct`, `lower_bound_pct`, `grid_count`) are expressible via V2 fields and do not require special handling.
- The rename is `GridConfigV2` 
 `GridConfig` and `GridCalculatorV2` functions 
 `GridCalculator` methods. Version suffixes are dropped entirely.
- `OptunaGridOptimizer` is the only non-module consumer of `GridConfigV2`. No other file outside `modules/grid_trading/` imports from `grid_calculator_v2.py`.
- The `IAlgoModule` interface contract is unchanged by this feature.
- `research/gym_origin/grid_trading_gym_v2.py` is a research artifact and is not updated as part of this feature.
- Existing tests for grid trading are testing behaviour, not implementation internals, so they will pass against the unified implementation without modification.

---

## Out of Scope

- Changing the `IAlgoModule` interface or adding new lifecycle hooks.
- Adding new grid spacing methods beyond what currently exists in `grid_calculator_v2.py`.
- Migrating `research/gym_origin/grid_trading_gym_v2.py` to the unified API.
- Freqtrade hyperopt integration for grid parameters.
- Real-money live trading validation.

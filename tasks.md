# Tasks: Layered Algorithmic Trading System (LATS)

**Input**: Design documents from `/specs/001-lats-algo-trading/`
**Prerequisites**: plan.md ✅ spec.md ✅ research.md ✅ data-model.md ✅ contracts/ ✅

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US8)
- All paths relative to `user_data/strategies/algo_system/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create package skeleton — all directories, `__init__.py` files, and the config stub needed before any module can be written.

- [x] T001 Create full directory tree: `user_data/strategies/algo_system/{orchestrator,base,modules/grid_trading,reasoning,safety,config,observability,tests/harness}/`
- [x] T002 [P] Create `user_data/strategies/algo_system/__init__.py` and all sub-package `__init__.py` files (one per directory from T001)
- [x] T003 [P] Create `user_data/config_algo_backtest.json` — sample freqtrade config enabling OrchestratorStrategy with grid module, 6-month BTC/USDT + ETH/USDT data, `position_adjustment_enable: true`, and the `algo_system` config block skeleton (modules_path, active_modules, circuit_breaker, budget entries)

**Checkpoint**: `python -c "import user_data.strategies.algo_system"` succeeds without ImportError.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: All abstract interfaces, base dataclasses, and zero-dependency infrastructure. Every user story depends on this phase completing first.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete. Wave 1 and Wave 2 from plan.md.

### Wave 1 — Zero-dependency foundations (all parallel)

- [x] T004 [P] Implement `base/ialgo_module.py` — `ModuleCapability` Enum, `ModuleState` Enum, `VALID_TRANSITIONS` dict, `ModuleSignal` dataclass, `IAlgoModule` ABC with all abstract method signatures from `contracts/ialgo_module_contract.md`
- [x] T005 [P] Implement `base/module_context.py` — `DataProviderProxy` facade (wraps freqtrade `IDataProvider`), `WalletsProxy` facade (wraps freqtrade `Wallets`), `ModuleContext` dataclass (pair, run_mode, current_time, data_provider, wallets, shared_state, reasoning_hints, logger, module_id)
- [x] T006 [P] Implement `orchestrator/shared_state.py` — `ModuleStateEntry` TypedDict, `SharedState` class with `get/set/delete` keyed by `(module_id, pair)`, `threading.Lock`, JSON save/load at `user_data/algo_system_state.json`
- [x] T007 [P] Implement `safety/circuit_breaker.py` — `CircuitBreakerStatus` Enum (`ARMED/TRIPPED/COOLING`), `CircuitBreakerState` dataclass, `CircuitBreaker` class with `evaluate(df, current_drawdown_pct)` → bool, `trip(reason)`, `reset()` methods; thresholds: `max_drawdown_pct`, `price_move_pct`, `price_move_candles` from config

### Wave 2 — Depend on Wave 1 only (all parallel)

- [x] T008 [P] Implement `orchestrator/algo_lifecycle.py` — `AlgoLifecycleSM` managing `Dict[str, Dict[str, ModuleState]]` (module_id → pair → state); `transition(module_id, pair, new_state)` validating against `VALID_TRANSITIONS`; `MANAGED_STATES` check helpers
- [x] T009 [P] Implement `orchestrator/budget_allocator.py` — `ModuleBudget` dataclass, `BudgetAllocator` class with `register_module(module_id, max_open_trades, stake_pct)`, `can_open_trade(module_id, proposed_stake, total_capital)` → bool, `record_open(module_id, stake)`, `record_close(module_id, stake)`
- [x] T010 [P] Implement `orchestrator/signal_arbiter.py` — `SignalArbiter` with priority list (`priority_order: List[str]`), `arbitrate(signals: Dict[str, ModuleSignal], routing_decision: Optional[RoutingDecision])` → `Tuple[str, ModuleSignal]` (winner_id, winning_signal); logs all resolutions with rationale
- [x] T011 [P] Implement `reasoning/reasoning_interface.py` — `IReasoningEngine` ABC with `initialize`, `score_modules`, `get_routing_decision`, `shutdown` from `contracts/reasoning_engine_contract.md`; `RoutingDecision` dataclass with `is_expired()` and `tick()` methods
- [x] T012 Implement `config/algo_config_schema.py` — `AlgoSystemConfig` TypedDict, `ReasoningConfig`, `CircuitBreakerConfig`, `SharedStateConfig`, `ObservabilityConfig`; `validate_algo_config(config: dict) -> AlgoSystemConfig` raising `ValueError` on missing/invalid keys — called at bot startup

**Checkpoint**: `pytest user_data/strategies/algo_system/tests/ -k "not test_" --collect-only` shows no import errors.

---

## Phase 3: User Story 1 — Grid Trading in Backtest Mode (Priority: P1) 🎯 MVP

**Goal**: Run a 6-month backtest with grid trading active. Produce a trade report showing grid-level entries/exits with correct capital isolation. No exchange connection needed.

**Independent Test**: `freqtrade backtesting --config user_data/config_algo_backtest.json --strategy OrchestratorStrategy --timerange 20230601-20231201` completes without error and shows grid-attributed trades.

### Wave 3 — Grid math (parallel)

- [x] T013 [P] [US1] Implement `modules/grid_trading/grid_calculator.py` — `GridCalculator` (stateless): `calculate_levels(lower, upper, count) -> List[float]` (evenly spaced ascending), `get_crossed_levels_down(from_price, to_price, levels) -> List[float]` (buy triggers), `get_crossed_levels_up(from_price, to_price, levels, filled) -> List[float]` (sell triggers), `validate_grid(lower, upper, count)` raising `ValueError` if `count < 2` or bounds invalid
- [x] T014 [P] [US1] Implement `modules/grid_trading/grid_state.py` — `GridState` dataclass: `pair, upper_bound, lower_bound, grid_count, grid_levels, filled_levels, initial_entry_price, created_at, last_updated`; `is_level_filled`, `mark_filled`, `mark_unfilled`, `is_in_range` methods; `to_dict()` / `from_dict()` for SharedState persistence

### Wave 4 — Assembly (sequential)

- [x] T015 [US1] Implement `modules/grid_trading/grid_trading_module.py` — `GridTradingModule(IAlgoModule)`: class vars (`module_id="grid_trading_v1"`, all `ModuleCapability` flags); `initialize` validates config and instantiates `GridCalculator`; `populate_indicators` adds `_grid_trading_v1_adx` column via `talib.ADX(14)`; `generate_entry_signal` returns initial entry signal when no grid exists; `adjust_position` calls calculator to detect level crossings and returns stake/partial-sell tuples; `on_order_filled` calls `grid_state.mark_filled(level)`; `shutdown` persists all GridState to SharedState
- [x] T016 [US1] Implement `orchestrator/module_registry.py` — `ModuleRegistry`: `register(module: IAlgoModule)`, `get_active(module_id)`, `get_all_active() -> List[IAlgoModule]`; calls `module.initialize(context)` on registration; enforces `supports_backtest/paper/live` capability check against current `RunMode`
- [x] T017 [US1] Implement `orchestrator/orchestrator_strategy.py` (Phase 1 skeleton) — `OrchestratorStrategy(IStrategy)`: `position_adjustment_enable = True`; `__init__` reads `config["algo_system"]`, calls `validate_algo_config`, instantiates `ModuleRegistry`, `SignalArbiter`, `BudgetAllocator`, `AlgoLifecycleSM`, `CircuitBreaker`, `SharedState`, loads `RuleBasedReasoningEngine` as default; `populate_indicators` fan-out to all active modules via `_safe_module_call`; `populate_entry_trend` / `populate_exit_trend` delegating to arbiter output; `adjust_trade_position` routing to module with signal authority; `_safe_module_call` mirroring `strategy_safe_wrapper` (catch-all, record failure, return default)

### Tests for User Story 1

- [x] T018 [P] [US1] Implement `tests/conftest.py` — `MockModuleContext` factory (creates `ModuleContext` with mock DataProvider, mock Wallets, in-memory SharedState), `make_ohlcv_df(n_candles, price_range)` helper producing realistic DataFrame, `MockDataProvider`, `MockWallets` stubs
- [x] T019 [P] [US1] Implement `tests/harness/mock_exchange.py` — minimal exchange stub satisfying freqtrade's `IExchange` for backtest isolation (order book, balance, order fill simulation)
- [x] T020 [P] [US1] Implement `tests/test_grid_module.py` — test `calculate_levels` spacing, test `get_crossed_levels_down` for price falling through multiple levels, test `get_crossed_levels_up` for filled levels, test `GridState.mark_filled/unfilled`, test `GridTradingModule.adjust_position` returns correct stake for a level crossing, test out-of-range price produces `None`
- [x] T021 [US1] Implement `tests/harness/backtest_harness.py` — programmatic `freqtrade.optimize.backtesting.Backtesting` runner loading `user_data/config_algo_backtest.json`; asserts trade count > 0, all enter_tags prefixed `"grid_trading_v1:"`, no module exceeds stake cap

**Checkpoint**: `pytest tests/test_grid_module.py` passes AND `backtest_harness.py` runs to completion on BTC/USDT 6-month slice.

---

## Phase 4: User Story 2 — Grid Trading in Paper Trading Mode (Priority: P1)

**Goal**: Run in dry-run mode for 1+ hours. Each action attributed to module+pair in logs. Circuit breaker alert fires within 1 candle.

**Independent Test**: `freqtrade trade --config user_data/config_algo_backtest.json --strategy OrchestratorStrategy --dry-run` runs for 1 hour without crash, logs show `[grid_trading_v1:BTC/USDT]` prefixed entries.

- [x] T022 [P] [US2] Implement `observability/module_logger.py` — `get_module_logger(module_id: str, pair: str) -> logging.Logger` factory returning logger named `algo_system.{module_id}.{pair}`; all module methods receive this logger via `ModuleContext`
- [x] T023 [P] [US2] Implement `observability/metrics_collector.py` — `MetricsCollector` wrapping `DataProvider.send_msg()`: `send_suspension_alert(module_id, pair, reason)`, `send_circuit_breaker_alert(reason, drawdown_pct)`, `send_routing_change(from_module, to_module, pair, rationale)` — all route to freqtrade Telegram/webhook via `dp.send_msg(msg, always_send=True)`
- [x] T024 [US2] Extend `orchestrator/orchestrator_strategy.py` with paper-trading hooks: `bot_start` (calls `module.on_bot_start(ctx)` for all modules), `order_filled` (routes to owning module's `on_order_filled`), integrate `MetricsCollector` for suspension alerts, wire `dp` reference into `MetricsCollector` after DataProvider is ready
- [x] T025 [US2] Extend `orchestrator/shared_state.py` disk persistence: `load_from_disk()` on bot start restoring grid states for all pairs, `save_to_disk()` called in `shutdown()` and after each `set()` operation (debounced — max 1 write/candle); verify restart recovery test in `tests/conftest.py`

**Checkpoint**: Bot runs in dry-run for 5 minutes (local smoke test), logs contain structured entries, no unhandled exceptions.

---

## Phase 5: User Story 3 — Circuit Breaker (Priority: P1)

**Goal**: Circuit breaker triggers on extreme price move or drawdown. No new entries after trigger. Existing stoplosses unaffected.

**Independent Test**: Backtest on a historical flash-crash slice shows zero new entry orders after the trigger candle. Open-position stoplosses remain in trade records.

- [x] T026 [US3] Complete `safety/circuit_breaker.py` evaluation logic: `evaluate(df: DataFrame, portfolio_drawdown_pct: float)` checking (a) `price_move_pct` within `price_move_candles` window and (b) `portfolio_drawdown_pct >= max_drawdown_pct`; returns `True` (trip) with `trip_reason` string; `COOLING` state transitions with configurable reset candle count
- [x] T027 [US3] Wire `CircuitBreaker` into `orchestrator/orchestrator_strategy.py`: called at top of `populate_entry_trend` and `adjust_trade_position`; when tripped, returns `DataFrame` with no signals set / returns `None` from adjust; calls `MetricsCollector.send_circuit_breaker_alert()`; does NOT modify `custom_stoploss` output
- [x] T028 [US3] Implement `tests/test_circuit_breaker.py` — test price-move threshold triggers, test drawdown threshold triggers, test ARMED→TRIPPED→COOLING state transitions, test that tripped state produces no signals in orchestrator populate methods, test stoploss column unaffected after trip

**Checkpoint**: `pytest tests/test_circuit_breaker.py` passes. Flash-crash backtest slice shows CB trip in correct candle.

---

## Phase 6: User Story 4 — Per-Module Capital Isolation (Priority: P2)

**Goal**: Each module enforces its configured trade quota and stake cap. Quota rejections logged. Suspended module quota not redistributed.

**Independent Test**: Backtest with two modules both configured to 50% stake cap: neither exceeds its limit across any 1000-candle window.

- [x] T029 [US4] Complete `orchestrator/budget_allocator.py`: `can_open_trade` checking both `max_open_trades` and `stake_allocation_pct * total_capital`; `record_suspension(module_id)` marking module as suspended (prevents quota transfer); `get_utilization(module_id)` → `Dict` for observability; all rejections logged with reason
- [x] T030 [US4] Wire `BudgetAllocator` into `orchestrator/orchestrator_strategy.py` `confirm_trade_entry`: call `budget_allocator.can_open_trade(module_id, proposed_stake, wallets.get_total_stake_amount())` before approving; call `record_open` on fill, `record_close` on trade close via `order_filled` hook
- [x] T031 [US4] Extend `tests/test_orchestrator.py` with budget enforcement tests: quota cap blocks 11th trade when limit=10, stake cap blocks order when utilization full, suspended module quota stays reserved, rejection logged

**Checkpoint**: `pytest tests/test_orchestrator.py -k budget` passes.

---

## Phase 7: User Story 5 — Rule-Based Reasoning Selects the Right Module (Priority: P2)

**Goal**: In ranging markets, grid module is selected as authoritative. In trending markets, system holds. Routing change logged with rationale.

**Independent Test**: Backtest comparison of routing decisions vs. ADX values — grid selected when ADX < 25 in ≥ 70% of candles confirming ranging condition.

- [x] T032 [P] [US5] Implement `reasoning/rule_based_reasoning.py` — `RuleBasedReasoningEngine(IReasoningEngine)`: `score_modules(pair, df, active_module_ids)` returning `Dict[str, float]`; scoring: `+0.7` if `df["adx"].iloc[-1] < 25` (talib ADX-14), `+0.3` if `bb_width < 0.04` (`(bb_upper - bb_lower) / bb_mid` via `qtpylib.bollinger_bands()`), `+0.1` if `40 < rsi < 60` (talib RSI-14), clamped to 1.0; `get_routing_decision` checks TTL via `RoutingDecision.is_expired()` before re-evaluating
- [x] T033 [US5] Wire `RuleBasedReasoningEngine` into `orchestrator/orchestrator_strategy.py`: call `reasoning_engine.get_routing_decision(pair, df)` in `populate_indicators`; update `ModuleContext.reasoning_hints`; pass `RoutingDecision` to `SignalArbiter.arbitrate()`; call `reasoning_engine.tick()` each candle; emit `MetricsCollector.send_routing_change()` on authority change
- [x] T034 [US5] Implement `tests/test_reasoning.py` — test ADX<25 + narrow BB produces score > 0.7, test ADX>25 produces score < 0.3, test TTL expiry forces re-evaluation, test `get_routing_decision` returns None when all scores below threshold, test rationale string non-empty on routing change

**Checkpoint**: `pytest tests/test_reasoning.py` passes. Backtest routing log shows grid selected in confirmed ranging candles at ≥ 70% rate.

---

## Phase 8: User Story 6 — Entry Timing and Discounted Entry Quality (Priority: P2)

**Goal**: Grid initialization gated by entry quality score. No grid opened when price is at a relative extreme or after a sharp directional move.

**Independent Test**: Backtest on a trending-then-ranging period: grids are NOT initialized during trending phase and ARE initialized promptly after ranging is confirmed.

- [x] T035 [P] [US6] Implement `reasoning/entry_quality_evaluator.py` — `EntryQualityEvaluator`: `score(pair, df, config) -> Tuple[float, str]` evaluating (a) momentum: if `abs(df["close"].pct_change(N).iloc[-1]) > momentum_threshold` → penalize score; (b) mean-reversion: if `close` is within `mid_pct` of N-candle range midpoint → boost score; (c) neutral RSI (40–60) → boost score; returns 0.0–1.0 + rationale string
- [x] T036 [US6] Wire `EntryQualityEvaluator` into `modules/grid_trading/grid_trading_module.py` `generate_entry_signal`: when no grid exists for pair, call `entry_quality_evaluator.score()` → if below `entry_quality_threshold` (config), return `ModuleSignal()` (no opinion) and log deferred reason; if above threshold, proceed with grid initialization; does NOT affect `adjust_position` on already-active grids
- [x] T037 [US6] Add `entry_quality` block to `config/algo_config_schema.py`: `momentum_lookback_candles: int`, `momentum_threshold: float`, `entry_quality_threshold: float`, `max_defer_candles: int` (emit warning after this many consecutive deferrals); add validation in `validate_algo_config`

**Checkpoint**: Backtest on 2023-11 BTC trending period: zero grid initializations. Backtest on 2023-04 ranging period: grid opens within first 5 candles of stable ADX<25 zone.

---

## Phase 9: User Story 7 — Dynamic Pair Selection (Priority: P2)

**Goal**: Modules only activate on pairs matching their market characteristics. Pair removal triggers draining, not force-close.

**Independent Test**: Backtest with BTC (trending) and ETH (ranging) where only ETH qualifies — grid module shows zero activity on BTC, active grid on ETH.

- [x] T038 [P] [US7] Implement `orchestrator/pair_selector.py` — `PairSelectorCriteria` dataclass (min_daily_volume, max_adx, min_bb_width, max_bb_width); `PairSelector`: `evaluate_pair(pair, df, criteria) -> Tuple[bool, str]` checking each criterion; `update_active_pairs(module_id, whitelist, df_map) -> Tuple[List[str], List[str]]` (added, removed); re-evaluation interval configurable in candles
- [x] T039 [US7] Integrate `PairSelector` into `orchestrator/module_registry.py`: `get_active_pairs_for_module(module_id) -> List[str]`; on `update_active_pairs`, for each removed pair call `AlgoLifecycleSM.transition(module_id, pair, ModuleState.DRAINING)` if pair has open positions; log additions/removals
- [x] T040 [US7] Add `pair_selection` block to `config/algo_config_schema.py`: `enabled: bool`, `evaluation_interval_candles: int`, `criteria` per module (min_daily_volume, max_adx, min_bb_width); validate in `validate_algo_config`
- [x] T041 [US7] Extend `tests/test_orchestrator.py` with pair selection tests: trending pair excluded, ranging pair included, removal of active pair triggers draining not force-close, re-evaluation on interval restores qualified pair

**Checkpoint**: `pytest tests/test_orchestrator.py -k pair_selector` passes. Backtest confirms BTC (ADX>25) excluded, ETH included.

---

## Phase 10: User Story 8 — Plugin Discovery Without Touching Orchestrator (Priority: P2)

**Goal**: Drop a new module file + config entry → bot discovers and initializes it. No orchestrator code changes. Signal conflict resolved and logged. Module suspension auto-fires after 3 failures.

**Independent Test**: A developer copies the stub from `ialgo_module_contract.md` into `modules/my_stub/my_stub_module.py`, adds it to config `active_modules`, starts the bot — module appears in initialization logs with no other code changes.

- [x] T042 [P] [US8] Implement `orchestrator/module_resolver.py` — `ModuleResolver`: `discover_modules(modules_path: str) -> List[Type[IAlgoModule]]` using `importlib.util.spec_from_file_location` + `inspect.getmembers` to find all `IAlgoModule` subclasses not equal to `IAlgoModule` in the scanned files (mirrors `IResolver._get_valid_object` pattern from research R-011)
- [x] T043 [US8] Update `orchestrator/module_registry.py` to use `ModuleResolver`: on `discover_and_register(modules_path, active_module_ids, context)` call `resolver.discover_modules(modules_path)`, filter to `active_module_ids`, instantiate and register each; reject modules whose `RunMode` capability is `UNSUPPORTED` for current run mode
- [x] T044 [US8] Implement `orchestrator/orchestrator_strategy.py` auto-suspension: `_record_module_failure(module_id, pair)` increments failure counter; after 3 consecutive failures → `AlgoLifecycleSM.transition(SUSPENDED)`, call `MetricsCollector.send_suspension_alert()`, stop routing to that module; `reset_module_failure_count(module_id)` on successful call
- [x] T045 [US8] Plugin smoke test in `tests/test_orchestrator.py`: write a minimal stub file to a temp directory, point config at it, run `discover_and_register` → assert stub appears in registry; test 3-failure suspension fires alert and removes module from routing; test two conflicting signals resolved by arbiter with logged rationale

**Checkpoint**: `pytest tests/test_orchestrator.py -k discovery` passes. Manual test: drop stub file, start bot — stub appears in logs.

---

## Phase 11: Polish & Cross-Cutting Concerns

**Purpose**: Hardening, observability, and validation that spans all stories.

- [x] T046 [P] Add inline docstrings to all public classes and methods in `base/`, `orchestrator/`, `reasoning/`, `safety/` — focus on contract semantics (what, not how)
- [x] T047 [P] Validate all `enter_tag` values across `GridTradingModule` and any stub modules conform to `"{module_id}:{reason}"` format (max 255 chars) — add assertion in `_safe_module_call` for debug mode
- [x] T048 Run full `pytest user_data/strategies/algo_system/tests/` — assert all tests pass in under 60 seconds
- [x] T049 Run `backtest_harness.py` end-to-end on BTC/USDT + ETH/USDT 6-month slice — assert: (a) completes in < 5 min, (b) all enter_tags prefixed correctly, (c) no module exceeds stake cap
- [x] T050 [P] Create `user_data/strategies/algo_system/README.md` documenting: how to add a new module (5-step guide using the stub), config schema reference, circuit breaker thresholds, entry quality config fields

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 — first deliverable (backtest MVP)
- **Phase 4 (US2)**: Depends on Phase 3 (needs orchestrator skeleton from T017)
- **Phase 5 (US3)**: Depends on Phase 2 — can start in parallel with US1/US2 after Phase 2
- **Phase 6 (US4)**: Depends on Phase 2 — can parallelize with Phase 5
- **Phase 7 (US5)**: Depends on Phase 3 (needs populate_indicators in orchestrator)
- **Phase 8 (US6)**: Depends on Phase 3 (grid module must exist) and Phase 7 (ADX/BB already computed)
- **Phase 9 (US7)**: Depends on Phase 2 (AlgoLifecycleSM) and Phase 3 (ModuleRegistry)
- **Phase 10 (US8)**: Depends on Phase 2 (ModuleRegistry skeleton) and Phase 4 (suspension logic)
- **Phase 11 (Polish)**: Depends on all prior phases complete

### User Story Dependencies

```
Phase 2 (Foundation)
  ├── US1 (backtest MVP) ──► US2 (paper trading) ──► US8 (plugin discovery)
  ├── US3 (circuit breaker) [parallel with US1/US2]
  ├── US4 (capital isolation) [parallel with US3]
  ├── US1 ──► US5 (reasoning) ──► US6 (entry timing)
  └── US1 ──► US7 (pair selection)
```

### Within Each User Story

- For US1/US2/US3: Core logic → orchestrator wiring → tests
- For US5/US6/US7: Implementation (T_xx[P]) → orchestrator wiring → config extension
- [P]-marked tasks within a phase have no mutual dependencies and can run in parallel

---

## Parallel Execution Examples

### Phase 2 Wave 1 (T004–T007 all parallel)

```
Agent 1: T004 — base/ialgo_module.py
Agent 2: T005 — base/module_context.py
Agent 3: T006 — orchestrator/shared_state.py
Agent 4: T007 — safety/circuit_breaker.py
```

### Phase 2 Wave 2 (T008–T011 all parallel after T004–T007)

```
Agent 1: T008 — orchestrator/algo_lifecycle.py
Agent 2: T009 — orchestrator/budget_allocator.py
Agent 3: T010 — orchestrator/signal_arbiter.py
Agent 4: T011 — reasoning/reasoning_interface.py
```

### Phase 3 Wave 3 (T013–T014 parallel)

```
Agent 1: T013 — grid_calculator.py (pure math)
Agent 2: T014 — grid_state.py (pure dataclass)
```

### Phase 3 Tests (T018–T020 parallel after T017)

```
Agent 1: T018 — tests/conftest.py
Agent 2: T019 — tests/harness/mock_exchange.py
Agent 3: T020 — tests/test_grid_module.py
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T003)
2. Complete Phase 2: Foundational — Wave 1 parallel (T004–T007), then Wave 2 parallel (T008–T012)
3. Complete Phase 3: US1 — Wave 3 parallel (T013–T014), then sequential (T015–T021)
4. **STOP and VALIDATE**: `backtest_harness.py` runs cleanly, `pytest test_grid_module.py` passes
5. Demo: 6-month BTC/USDT grid backtest with trade report ✅

### Incremental Delivery

| Step | Phase | Deliverable | Validation |
|------|-------|-------------|------------|
| 1 | Phase 1+2 | Foundation ready | Import check passes |
| 2 | Phase 3 | Grid backtest MVP | Backtest harness green |
| 3 | Phase 4 | Paper trading | 1-hour dry-run clean |
| 4 | Phase 5 | Circuit breaker | CB test + flash-crash backtest |
| 5 | Phase 6 | Capital isolation | Budget quota tests pass |
| 6 | Phase 7 | Reasoning engine | Regime detection ≥ 70% accuracy |
| 7 | Phase 8 | Entry timing | No grid opens in trending backtest period |
| 8 | Phase 9 | Pair selection | BTC excluded, ETH included backtest |
| 9 | Phase 10 | Plugin discovery | Stub auto-discovered from temp dir |
| 10 | Phase 11 | Polish | Full test suite < 60s, docs complete |

### Parallel Team Strategy (2 developers post-Foundation)

**After Phase 2 completes:**
- **Dev A**: Phase 3 (US1) → Phase 4 (US2) → Phase 8 (US6)
- **Dev B**: Phase 5 (US3) → Phase 7 (US5) → Phase 9 (US7)
- **Phase 6 (US4) and Phase 10 (US8)**: Either dev after their track completes

---

## Notes

- [P] tasks touch different files — safe to parallelize with no merge conflicts
- All paths are under `user_data/strategies/algo_system/` (never in freqtrade core)
- Use `talib.ADX()` + `qtpylib.bollinger_bands()` — NOT pandas-ta (research decision R-012)
- `enter_tag` format: `"{module_id}:{reason}"` max 255 chars (research decision R-002)
- `adjust_trade_position` is the ONLY grid order mechanism (research decision R-001)
- `dp.send_msg()` is the ONLY alert channel — routes automatically to Telegram/webhook
- Commit after each checkpoint (T021, T025, T028, T031, T034, T037, T041, T045, T050)
- Never modify freqtrade core files — all work stays in `user_data/strategies/algo_system/`

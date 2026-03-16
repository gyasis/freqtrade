# Feature Specification: Layered Algorithmic Trading System (LATS)

**Feature Branch**: `001-lats-algo-trading`
**Created**: 2026-03-16
**Status**: Draft
**PRD Reference**: `prd/layered-algo-trading-system.md`

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Grid Trading in Backtest Mode (Priority: P1)

As a trader, I want to backtest the grid trading algorithm against historical crypto market data so I can evaluate its performance before risking real capital.

**Why this priority**: Backtesting is the first gate before any live deployment. Without a working backtest, nothing else can be validated. This is a self-contained deliverable with immediate standalone value.

**Independent Test**: Can be fully tested by running a backtest command with OrchestratorStrategy and reviewing the trade report — no exchange connection or live capital needed.

**Acceptance Scenarios**:

1. **Given** a configured system with grid trading active, **When** a backtest is run for a 6-month historical period, **Then** it completes without errors and produces a trade report showing grid-level entries and exits.
2. **Given** a ranging market in historical data, **When** the grid module processes candles, **Then** position adjustments appear at each grid level crossing in the backtest output.
3. **Given** a configured capital budget per module, **When** the backtest completes, **Then** no module exceeds its configured stake allocation in any candle.
4. **Given** price moving outside the configured grid range, **When** the grid module evaluates the candle, **Then** no new grid orders are placed outside the defined bounds.

---

### User Story 2 — Grid Trading in Paper Trading Mode (Priority: P1)

As a trader, I want to run the system against live market data in simulation mode so I can validate real-time behavior without financial risk.

**Why this priority**: Paper trading validates that the system works with live data feeds, exchange connectivity, and real-time candle timing — things backtest cannot fully replicate.

**Independent Test**: Can be fully tested by running the bot in dry-run mode for one hour and reviewing the simulated wallet and trade log for grid-level activity.

**Acceptance Scenarios**:

1. **Given** the system running in dry-run mode, **When** a new candle arrives, **Then** the grid module evaluates price against grid levels and places simulated orders accordingly.
2. **Given** the bot has been running for 1+ hours, **When** reviewing logs, **Then** each action is attributed to a specific module and pair with structured log entries.
3. **Given** a sudden large price move exceeding the circuit breaker threshold, **When** the circuit breaker fires, **Then** no new orders are placed and an alert is emitted within one candle.

---

### User Story 3 — Circuit Breaker Halts All Trading on Market Crisis (Priority: P1)

As a trader, I want the system to automatically halt all new trading activity during extreme market conditions regardless of which module is active.

**Why this priority**: Capital preservation during black swan events takes precedence over all algorithm logic. This must work before live trading is considered.

**Independent Test**: Can be fully tested in backtest by identifying a historical flash-crash period and confirming zero new entry orders appear after the trigger candle.

**Acceptance Scenarios**:

1. **Given** price moves beyond the configured percentage within the configured candle window, **When** the circuit breaker detects the move, **Then** all module signal authority is revoked and no new orders are placed.
2. **Given** portfolio drawdown exceeds the configured maximum, **When** the circuit breaker triggers, **Then** the system emits an alert and enters halt state.
3. **Given** the circuit breaker is active and open positions have stoplosses, **When** reviewing open trades, **Then** all existing stoplosses remain intact — the circuit breaker does not remove risk management.

---

### User Story 4 — Per-Module Capital Isolation (Priority: P2)

As a trader managing risk, I want each algorithm module to operate within its own capital budget so one module cannot consume all available trade slots or funds.

**Why this priority**: Without budget isolation, a greedy module can starve all others and over-expose the portfolio. Required before any multi-module live operation.

**Independent Test**: Can be fully tested in backtest by configuring two modules with different budgets and confirming neither exceeds its quota across 1000 candles.

**Acceptance Scenarios**:

1. **Given** a module configured with a quota of 10 open trades, **When** it tries to open an 11th trade, **Then** the request is rejected and the rejection is logged.
2. **Given** a module configured with 50% stake allocation, **When** it places orders, **Then** its combined open position value never exceeds 50% of total capital.
3. **Given** one module is suspended, **When** reviewing the budget state, **Then** its quota remains reserved and is not silently redistributed to other modules.

---

### User Story 5 — Rule-Based Reasoning Selects the Right Module (Priority: P2)

As a trader, I want the system to automatically select the best-fit algorithm for current market conditions without requiring manual switching.

**Why this priority**: Automatic module selection is what separates this system from a plain single-strategy bot. Must work correctly before adding ML-based reasoning.

**Independent Test**: Can be fully tested in backtest by comparing the routing decision log against market regime indicators — grid selected during ranging periods, held during trending periods.

**Acceptance Scenarios**:

1. **Given** a market with low directional movement and narrow volatility range, **When** the reasoning engine evaluates modules, **Then** the grid module receives a score above the activation threshold and is granted signal authority.
2. **Given** a strongly trending market, **When** the reasoning engine scores modules, **Then** the grid module scores below the threshold and the system holds if no trending module is registered.
3. **Given** a routing decision that has exceeded its TTL, **When** the next candle arrives, **Then** the reasoning engine re-evaluates and may change module authority.

---

### User Story 6 — Entry Timing and Discounted Entry Quality (Priority: P2)

As a trader, I want the system to evaluate whether the current price represents a quality entry point before the grid module opens its initial position, so I'm not entering a grid at an unfavorable range.

**Why this priority**: A grid entered at the wrong price range (e.g., near a local top during a distribution phase) will have most levels filled immediately and incur significant drawdown. Entry quality scoring prevents wasted capital on poorly timed grid initializations.

**Independent Test**: Can be fully tested in backtest by comparing grid initialization candles against historical OHLCV context — grids should not initialize when price is at a relative extreme or after a sustained directional move.

**Acceptance Scenarios**:

1. **Given** a pair where price has moved up sharply in the preceding N candles, **When** the grid module evaluates a new grid initialization, **Then** the entry quality score is below the activation threshold and the grid is not opened.
2. **Given** a pair where price is near the midpoint of its recent N-candle range and momentum is neutral, **When** the grid module scores entry quality, **Then** the score is above threshold and grid initialization proceeds.
3. **Given** an already-active grid for a pair, **When** entry quality scoring runs, **Then** it does not affect the running grid — scoring applies only to new grid initialization decisions.

---

### User Story 7 — Dynamic Pair Selection Based on Market Conditions (Priority: P2)

As a trader, I want the system to automatically curate which trading pairs each module operates on based on market characteristics (volume, volatility, liquidity), so modules are only assigned to pairs where their strategy has edge.

**Why this priority**: Running grid trading on trending, illiquid, or low-volume pairs wastes capital and produces low-quality signals. Pair selection should be adaptive, not static.

**Independent Test**: Can be fully tested in backtest by configuring pair scoring thresholds and confirming that only qualifying pairs receive module assignments across a 30-day test window.

**Acceptance Scenarios**:

1. **Given** a pair exhibiting strong directional trend, **When** the pair selector evaluates it for the grid module, **Then** the pair is excluded from the grid module's active pair list.
2. **Given** a pair that was excluded becoming range-bound, **When** re-evaluation runs at the configured interval, **Then** the pair is added back to the grid module's active list.
3. **Given** a pair dropped from the active list while a grid position is open, **When** the pair selector deactivates it, **Then** the existing position enters draining state — it is not force-closed.

---

### User Story 8 — New Module Added Without Touching Orchestrator (Priority: P2)

As a strategy developer, I want to drop a new algorithm file into the modules directory and activate it via config without modifying any orchestrator or core system code.

**Why this priority**: Extensibility is the system's long-term value. If adding a new module requires touching the orchestrator, the system becomes a maintenance burden.

**Independent Test**: Can be fully tested by a developer unfamiliar with the codebase dropping in a stub module implementation, adding it to the config, and confirming it runs — with no other code changes.

**Acceptance Scenarios**:

1. **Given** a new file implementing the algorithm module interface placed in the modules directory, **When** the bot starts, **Then** the module is discovered and initialized automatically.
2. **Given** a new module producing an entry signal on the same pair as an existing module, **When** both signals arrive in the same candle, **Then** the signal arbiter resolves the conflict using priority rules and logs its decision.
3. **Given** a module that raises an exception 3 times consecutively, **When** the 3rd failure occurs, **Then** the module is auto-suspended, an alert is emitted, and all other modules continue operating normally.

---

### Edge Cases

- What happens when the bot restarts mid-grid? Grid state must be restored from disk — open grid levels must not be orphaned.
- What happens when an exchange order fails during a grid level placement? The failed level must not be marked as filled; retry logic applies.
- What happens when a module switch is triggered while grid positions are open? The draining state must keep the old module's stoplosses active until all its positions are confirmed closed.
- What happens when two modules produce identical entry signals on the same pair in the same candle? The signal arbiter selects one and logs the resolution rationale.
- What happens when the global `max_open_trades` limit is reached? The budget allocator blocks all modules from attempting new entries.
- What happens when no module scores above the activation threshold? The system holds — emits no signal — rather than defaulting to any module.
- What happens when grid range bounds are set too tightly? The grid calculator must validate that at least 2 levels fit within the range and reject invalid configurations at startup.
- What happens when entry quality scoring defers a grid for many candles because conditions never improve? A maximum defer count should prevent indefinite non-entry — after N deferred candles, the system should log a warning and the operator can investigate.
- What happens when all pairs are filtered out by the pair selector? The module becomes idle but does not error — it waits for pairs to qualify on the next evaluation cycle.
- What happens when a pair is in active pair list but the exchange whitelist does not include it? The pair selector can only work within the exchange-provided whitelist — it narrows, never expands beyond it.

---

## Requirements *(mandatory)*

### Functional Requirements

**Orchestrator**
- **FR-001**: The system MUST operate as a single valid strategy loadable by the freqtrade runtime without any modification to freqtrade core files.
- **FR-002**: The system MUST support backtest, paper trading, and live trading modes without code changes between modes.
- **FR-003**: The system MUST delegate all trade decisions to the module with current signal authority for each pair.
- **FR-004**: The system MUST isolate all module calls so a single module failure cannot crash the bot or affect other modules.
- **FR-005**: The system MUST auto-suspend a module after 3 consecutive call failures and emit an alert.

**Algorithm Modules**
- **FR-006**: Every algorithm module MUST implement the defined abstract module interface.
- **FR-007**: Every module MUST declare its supported operation modes (backtest, paper, live, etc.).
- **FR-008**: Every module MUST prefix all trade entry tags with its module identifier for attribution.
- **FR-009**: Module state MUST be isolated per trading pair — state from one pair MUST NOT affect another.
- **FR-010**: Modules MUST be activatable by placing a file in the modules directory and adding the module ID to config, with zero orchestrator code changes.

**Grid Trading Module**
- **FR-011**: Grid trading MUST be implemented via the position adjustment hook — NOT via separate entry signals per grid level.
- **FR-012**: Grid levels MUST be calculated as evenly spaced price intervals between a configurable lower and upper bound.
- **FR-013**: When price crosses a grid level downward and that level is unfilled, the module MUST trigger a buy at that level.
- **FR-014**: When price crosses a filled grid level upward, the module MUST trigger a partial position reduction.
- **FR-015**: Grid state per pair MUST be persisted to disk so a bot restart does not orphan open grid levels.

**Reasoning Layer**
- **FR-016**: The default reasoning engine MUST score modules using market regime indicators without requiring any ML model training.
- **FR-017**: The reasoning engine MUST produce a routing decision with a human-readable rationale logged each time module authority changes.
- **FR-018**: If no module scores above the configured threshold, the system MUST hold — no signal is emitted.
- **FR-019**: Routing decisions MUST have a configurable time-to-live after which re-evaluation is forced.

**Budget Allocator**
- **FR-020**: Each active module MUST have a configurable maximum trade count quota enforced independently.
- **FR-021**: Each active module MUST have a configurable stake percentage cap that prevents over-allocation of capital.
- **FR-022**: Entry requests exceeding a module's quota MUST be rejected and logged.

**Signal Arbiter**
- **FR-023**: Conflicting signals MUST be resolved using a defined priority hierarchy: emergency controls first, then reasoning engine decision, then module confidence score, then registration order.
- **FR-024**: Every conflict resolution MUST be logged with the winning module, losing modules, and rationale.

**Circuit Breaker**
- **FR-025**: The circuit breaker MUST trigger when price moves beyond a configurable percentage within a configurable candle window.
- **FR-026**: The circuit breaker MUST trigger when portfolio drawdown exceeds a configurable percentage.
- **FR-027**: When triggered, the circuit breaker MUST revoke all module signal authority but MUST NOT cancel existing stoplosses on open positions.

**Algorithm Lifecycle**
- **FR-028**: Each module per pair MUST follow a defined state machine — from inactive through active, draining, and switching states.
- **FR-029**: A module in the draining state MUST maintain stoploss management for its open positions until all are confirmed closed before completing a switch.
- **FR-030**: Module switches MUST respect a configurable cooldown period to prevent rapid thrashing.

**Entry Timing / Entry Quality**
- **FR-031**: Before opening the initial position in a new grid, the system MUST evaluate an entry quality score using recent price momentum and mean-reversion indicators.
- **FR-032**: If entry quality score is below a configurable threshold, the grid initialization MUST be deferred — no position opened — and re-evaluated on the next candle.
- **FR-033**: Entry quality scoring MUST NOT affect or interrupt an already-active grid — it applies only at grid initialization time.

**Pair Selection**
- **FR-034**: The system MUST support dynamic per-module pair filtering so modules only operate on pairs that match their required market characteristics.
- **FR-035**: A pair removed from a module's active set while a position is open MUST trigger a draining state for that pair — NOT a forced position close.
- **FR-036**: Pair selection criteria MUST be configurable per module (e.g., minimum daily volume, maximum ADX value for grid module, minimum volatility range).

### Key Entities

- **OrchestratorStrategy**: The single strategy the trading runtime loads. Owns all subsystems and delegates all trade actions.
- **IAlgoModule**: Abstract contract all algorithm modules implement. Defines signal production, position management, and lifecycle hooks.
- **ModuleContext**: Runtime environment passed to modules each candle tick — exposes market data, wallet state, shared storage, and reasoning hints.
- **ModuleSignal**: Typed output of a module's signal evaluation — includes direction, confidence score, and attribution tag.
- **GridState**: Per-pair mutable record for the grid module — tracks price levels, which levels are filled, and grid bounds.
- **RoutingDecision**: Output of the reasoning engine — identifies the authoritative module, its confidence, a human-readable rationale, and validity duration.
- **SharedState**: Cross-module key-value store keyed by module and pair, with optional disk persistence across restarts.
- **BudgetAllocator**: Enforces per-module stake and trade-slot quotas before any order is placed.
- **SignalArbiter**: Resolves conflicting signals from multiple modules using the defined priority hierarchy.
- **AlgoLifecycleSM**: Per-pair state machine governing module transitions to prevent orphaned positions during switches.
- **CircuitBreaker**: Independent safety layer evaluating raw price movement and drawdown every candle, overriding all module authority when thresholds are breached.
- **EntryQualityEvaluator**: Scores whether the current price is a favorable entry point for grid initialization using momentum and mean-reversion indicators. Returns a 0.0–1.0 score with rationale.
- **PairSelector**: Evaluates each candidate pair against module-specific market characteristic criteria (volume, trend strength, volatility). Produces an active pair list per module that is re-evaluated periodically.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A backtest run with grid trading active completes on 6 months of historical data in under 5 minutes.
- **SC-002**: The bot runs in paper trading mode for 24 consecutive hours without a crash, memory leak, or unhandled exception.
- **SC-003**: A developer unfamiliar with the orchestrator internals can add and activate a new stub module in under 30 minutes.
- **SC-004**: When a circuit breaker event occurs during a backtested crash period, zero new entry orders are placed within the trigger candle or any subsequent candle while the halt is active.
- **SC-005**: No module exceeds its configured capital allocation across any 1000-candle backtest window.
- **SC-006**: Module suspension and circuit breaker alerts are delivered within 1 candle of the triggering event in paper or live mode.
- **SC-007**: 100% of trades in the trade database carry an entry tag prefixed with the originating module's identifier, enabling unambiguous per-module performance attribution.
- **SC-008**: In backtested ranging market periods, the grid module is selected as the authoritative module in at least 70% of candles where the market regime indicators confirm a ranging condition.
- **SC-009**: All unit tests for the system pass in under 60 seconds using mock context objects without a live exchange connection.

---

## Assumptions

- freqtrade v2024.8-dev (develop branch) is the runtime — no older version compatibility required.
- A single bot process is used — multi-process orchestration is out of scope for this spec.
- The `position_adjustment_enable` feature is available on the target exchange (standard for most spot exchanges).
- The rule-based reasoning engine (Phase 1) requires no ML model training and works on day one. It uses `talib.ADX()` and `qtpylib.bollinger_bands()` — libraries already present in the freqtrade environment.
- CoinGecko market data (`pycoingecko 3.1.0`) is available for optional reasoning enrichment but is not required for Phase 1 delivery.
- EODHD API credentials are stored in `.env` (not hardcoded) and loaded at startup.
- Module state persistence uses the existing `user_data/` directory.
- Context7 library docs are available for all external APIs: `/eodhistoricaldata/eodhd-claude-skills` and `/man-c/pycoingecko`.

---

## Out of Scope

- Modifying freqtrade core source files.
- FreqAI or reinforcement-learning-based reasoning (separate Phase 3 spec).
- EODHD equity screener integration.
- Multi-exchange arbitrage or multi-process bot instances.
- Web UI for module configuration (uses existing freqtrade UI).
- Options, derivatives, or leveraged trading-specific logic.

---

## Dependencies

- freqtrade v2024.8-dev — runtime engine (no modifications)
- `talib` (TA-Lib C library) — ADX and regime indicators for rule-based reasoning (freqtrade standard; NOT pandas-ta)
- `technical` (qtpylib) — Bollinger Bands via `qtpylib.bollinger_bands()` (already in freqtrade dependencies)
- `pycoingecko 3.1.0` — optional pair selection enrichment and market data
- `python-dotenv` — environment variable loading for API keys
- Context7: `/eodhistoricaldata/eodhd-claude-skills` (EODHD API docs)
- Context7: `/man-c/pycoingecko` (CoinGecko API docs)

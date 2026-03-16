# PRD: Layered Algorithmic Trading System (LATS)
**Project:** freqtrade extension layer
**Version:** 1.0.0
**Date:** 2026-03-16
**Status:** Draft

---

## 1. Overview

### 1.1 Problem Statement

Freqtrade provides excellent infrastructure for running a single trading strategy but has no native mechanism for:
- Running multiple algorithm modules (grid, momentum, mean reversion, etc.) simultaneously
- Dynamically routing signal authority to the best-fit algorithm based on market conditions
- Isolating capital budgets, trade slots, and state per algorithm
- Training an AI reasoning layer to evaluate which algorithm to use when

The user requires a modular, plug-and-play trading system where new algorithm modules can be added without touching core infrastructure, and a reasoning layer (rule-based in Phase 1, ML/RL in Phase 3) governs which module has signal authority per pair per candle.

### 1.2 Solution

Build a **non-invasive extension layer** on top of freqtrade that presents as a single valid `IStrategy` to the freqtrade runtime, while internally orchestrating multiple pluggable algorithm modules through a reasoning layer.

Freqtrade core is **never modified**. All new code lives in `user_data/strategies/algo_system/`.

### 1.3 Goals

| Goal | Description |
|------|-------------|
| Modularity | New algo modules added without changing orchestrator |
| Mode support | Every module supports Backtest, Paper (dry_run), and Live |
| Reasoning | Rule-based (Phase 1) → FreqAI ML (Phase 3) governs module selection |
| Safety | Capital isolation, circuit breakers, orphan-position protection |
| Observability | Per-module metrics, Telegram notifications, structured logging |

### 1.4 Non-Goals

- Modifying freqtrade core source files
- Building a new exchange connector
- Supporting multiple simultaneous freqtrade bot processes (single process architecture)
- Real-time WebSocket order management (uses freqtrade's existing order loop)

---

## 2. Users and Stakeholders

| Role | Description | Needs |
|------|-------------|-------|
| Solo trader | Runs the bot on their own capital | Reliable execution, clear P&L per module, Telegram alerts |
| Strategy developer | Adds new algorithm modules | Clean IAlgoModule interface, isolated testing harness |
| Researcher | Backtests algorithm combinations | Per-module backtest reports, side-by-side comparison |

---

## 3. System Architecture

### 3.1 Layer Map

```
┌─────────────────────────────────────────────────────────┐
│  freqtrade Runtime (UNCHANGED)                          │
│   FreqtradeBot / Backtesting                            │
│   loads → OrchestratorStrategy (single IStrategy)      │
└────────────────────┬────────────────────────────────────┘
                     │ IStrategy interface boundary
┌────────────────────▼────────────────────────────────────┐
│  Layer 2 — Orchestrator                                 │
│   OrchestratorStrategy                                  │
│   ├─ ModuleRegistry      discover + activate modules    │
│   ├─ SignalArbiter        conflict resolution           │
│   ├─ BudgetAllocator      per-algo stake + trade slots  │
│   ├─ SharedState          cross-module KV store         │
│   └─ AlgoLifecycleSM      ACTIVE→DRAINING→SWITCHING     │
└────────────────────┬────────────────────────────────────┘
                     │ delegates via ModuleContext
┌────────────────────▼────────────────────────────────────┐
│  Layer 1 — Algorithm Modules                            │
│   IAlgoModule (ABC)                                     │
│   ├─ GridTradingModule    ← Module #1 (Phase 1)         │
│   ├─ MomentumModule       ← future                      │
│   └─ MeanReversionModule  ← future                      │
└────────────────────┬────────────────────────────────────┘
                     │ scored and routed by
┌────────────────────▼────────────────────────────────────┐
│  Layer 3 — Reasoning                                    │
│   ├─ RuleBasedReasoning   ADX + BB width (Phase 1)     │
│   ├─ FreqAIReasoning      trained per-algo ML (Phase 3) │
│   └─ CircuitBreaker       overrides ALL algos on crisis │
└─────────────────────────────────────────────────────────┘
```

### 3.2 File Structure

```
user_data/strategies/algo_system/
├── __init__.py
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator_strategy.py    # IStrategy freqtrade loads
│   ├── module_registry.py          # discover + register IAlgoModule classes
│   ├── signal_arbiter.py           # resolve conflicting signals
│   ├── budget_allocator.py         # per-algo stake_amount + max_open_trades
│   ├── algo_lifecycle.py           # state machine for module transitions
│   ├── module_resolver.py          # IResolver-style dynamic class loader
│   └── shared_state.py             # cross-module typed KV store
├── base/
│   ├── __init__.py
│   ├── ialgo_module.py             # ABC + ModuleCapability + ModuleSignal
│   └── module_context.py           # runtime facades over FT internals
├── modules/
│   ├── __init__.py
│   └── grid_trading/
│       ├── __init__.py
│       ├── grid_trading_module.py  # IAlgoModule implementation
│       ├── grid_calculator.py      # pure grid math (no freqtrade deps)
│       └── grid_state.py           # per-pair grid position state
├── reasoning/
│   ├── __init__.py
│   ├── reasoning_interface.py      # IReasoningEngine ABC
│   ├── rule_based_reasoning.py     # Phase 1 default
│   └── freqai_reasoning.py         # Phase 3 ML adapter
├── config/
│   ├── __init__.py
│   └── algo_config_schema.py       # TypedDict config schema + validation
├── observability/
│   ├── __init__.py
│   ├── module_logger.py            # namespaced per-module logger
│   └── metrics_collector.py        # emit metrics to RPC/Telegram
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_grid_module.py
    ├── test_orchestrator.py
    ├── test_reasoning.py
    └── harness/
        ├── backtest_harness.py     # programmatic per-module backtest runner
        └── mock_exchange.py
```

---

## 4. Functional Requirements

### 4.1 Algorithm Module Interface

**FR-1:** Every algorithm module MUST implement `IAlgoModule` ABC with the following interface:

```python
class IAlgoModule(ABC):
    # Static identity
    module_id: ClassVar[str]
    version: ClassVar[str]
    capabilities: ClassVar[Set[ModuleCapability]]
    required_timeframes: ClassVar[List[str]]

    # Lifecycle
    def initialize(self, context: ModuleContext) -> None: ...
    def on_bot_start(self, context: ModuleContext) -> None: ...
    def shutdown(self, context: ModuleContext) -> None: ...

    # Per-candle signal production
    def populate_indicators(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> DataFrame: ...
    def generate_entry_signal(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal: ...
    def generate_exit_signal(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal: ...

    # Trade management (optional override)
    def adjust_position(self, trade, current_time, current_rate, current_profit,
                        min_stake, max_stake, ctx: ModuleContext) -> Optional[float]: ...
    def custom_stoploss(self, pair, trade, current_time, current_rate,
                        current_profit, ctx: ModuleContext) -> Optional[float]: ...
    def on_order_filled(self, pair, trade, order, current_time, ctx: ModuleContext) -> None: ...

    # Introspection
    def get_module_state(self, pair: str) -> Dict: ...
    def reset_module_state(self, pair: str) -> None: ...
```

**FR-2:** `ModuleCapability` enum MUST include: `BACKTEST`, `PAPER`, `LIVE`, `HYPEROPT`, `POSITION_ADJUST`, `SHORT`

**FR-3:** `ModuleSignal` MUST carry: `enter_long`, `enter_short`, `exit_long`, `exit_short`, `entry_tag`, `exit_tag`, `confidence (0.0–1.0)`, `metadata dict`

**FR-4:** `ModuleContext` MUST expose: `pair`, `run_mode`, `current_time`, `wallets` (facade), `data_provider` (facade), `shared_state`, `reasoning_hints`, `logger`

### 4.2 Orchestrator Strategy

**FR-5:** `OrchestratorStrategy` MUST be a valid `IStrategy` subclass loadable by `freqtrade` with `--strategy OrchestratorStrategy`

**FR-6:** `OrchestratorStrategy` MUST set `position_adjustment_enable = True` to support grid and DCA modules

**FR-7:** `OrchestratorStrategy.populate_indicators()` MUST call `populate_indicators()` on all active modules and merge their columns (prefixed `_{module_id}_`)

**FR-8:** `OrchestratorStrategy.populate_entry_trend()` MUST query the ReasoningEngine for a `RoutingDecision` before delegating to the authoritative module

**FR-9:** ALL module calls MUST be wrapped in exception isolation. A module raising an exception 3 consecutive times MUST be auto-suspended and an alert emitted

**FR-10:** `OrchestratorStrategy.adjust_trade_position()` MUST delegate to the module that opened the trade (via trade attribution in `enter_tag`)

### 4.3 Grid Trading Module (Module #1)

**FR-11:** `GridTradingModule` MUST implement grid trading entirely via `adjust_trade_position()` — NOT via separate entry signals per level

**FR-12:** Grid levels MUST be calculated by `GridCalculator` as a pure function: `calculate_levels(lower_bound, upper_bound, grid_count) -> List[float]`

**FR-13:** `GridState` per pair MUST track: `grid_levels`, `filled_levels`, `initial_entry_price`, `upper_bound`, `lower_bound`

**FR-14:** When price crosses a grid level downward and that level is unfilled → `adjust_position()` returns a positive stake to buy

**FR-15:** When price crosses a filled grid level upward → `adjust_position()` returns a negative stake to partially sell

**FR-16:** `generate_entry_signal()` fires only when price enters the grid range with no existing position (tag: `"grid:initial_entry"`)

**FR-17:** `generate_exit_signal()` fires when full grid completion condition is met OR circuit breaker is triggered

**FR-18:** All `enter_tag` values from GridTradingModule MUST be prefixed `"grid:"` for trade attribution

### 4.4 Reasoning Layer

**FR-19:** `IReasoningEngine` ABC MUST define: `score_modules(pair, df, active_modules) -> Dict[str, float]` and `get_routing_decision(pair, df) -> RoutingDecision`

**FR-20:** `RuleBasedReasoningEngine` (Phase 1 default) MUST score modules using ADX(14) and Bollinger Band width without any ML training:
- ADX < 25 AND BB narrow → grid score += 0.7
- ADX > 25 (trending) → grid score -= 0.5

**FR-21:** `RoutingDecision` MUST carry: `authoritative_module_id`, `confidence`, `rationale` (human-readable string), `valid_until_candle`

**FR-22:** If no module scores above `module_score_threshold` (config), the orchestrator MUST hold (no signal) rather than guess

**FR-23:** `CircuitBreaker` MUST override ALL module signals when any of the following are true:
- Price moved > N% in last M candles (configurable)
- Drawdown exceeds `max_drawdown_pct` (configurable)
- Exchange connectivity lost

### 4.5 Budget Allocator

**FR-24:** `BudgetAllocator` MUST enforce per-module `max_open_trades` quotas so no single module can consume all trade slots

**FR-25:** `BudgetAllocator` MUST enforce per-module `stake_allocation_pct` so no single module can consume more than its budget share

**FR-26:** Budget parameters MUST be configurable per module in the `algo_system` config block

### 4.6 Signal Arbiter

**FR-27:** `SignalArbiter` MUST resolve conflicting signals (e.g., Module A says enter_long, Module B says enter_short on same pair) using a priority hierarchy:
1. CircuitBreaker (highest)
2. ReasoningEngine routing decision
3. Module confidence score
4. Module order of registration (lowest)

**FR-28:** `SignalArbiter` MUST log all resolved conflicts with rationale

### 4.7 Algo Lifecycle State Machine

**FR-29:** Each active module per pair MUST follow the state machine:
```
INACTIVE → ACTIVE → DRAINING → SWITCHING → ACTIVE
                  ↓
               SUSPENDED (on repeated errors)
```

**FR-30:** A module MUST NOT have its stoploss management removed until all its positions are confirmed closed (DRAINING state completes)

**FR-31:** Module switches MUST be subject to `module_switch_cooldown_candles` (config) to prevent thrashing

### 4.8 Modes of Operation

**FR-32:** All modules declaring `ModuleCapability.BACKTEST` MUST produce valid signals when `dp.runmode == RunMode.BACKTEST`

**FR-33:** `freqtrade backtesting --strategy OrchestratorStrategy` MUST complete without errors with at least `grid_trading_v1` active

**FR-34:** `freqtrade trade --dry-run` MUST run for extended periods without crashes or memory leaks

**FR-35:** `freqtrade trade` (live) MUST only activate modules declaring `ModuleCapability.LIVE`

### 4.9 Configuration

**FR-36:** The system MUST be configured via an `algo_system` key in the standard freqtrade config JSON:

```json
{
    "strategy": "OrchestratorStrategy",
    "algo_system": {
        "modules_path": "user_data/strategies/algo_system/modules",
        "active_modules": ["grid_trading_v1"],
        "default_module": "grid_trading_v1",
        "module_switch_cooldown_candles": 5,
        "reasoning": {
            "engine": "rule_based",
            "freqai_enabled": false,
            "module_score_threshold": 0.4,
            "decision_ttl_candles": 3
        },
        "modules": {
            "grid_trading_v1": {
                "enabled": true,
                "grid_count": 10,
                "grid_range_pct": 0.05,
                "per_level_stake_pct": 0.1,
                "max_open_grid_levels": 5,
                "max_open_trades_quota": 10,
                "stake_allocation_pct": 0.5
            }
        },
        "circuit_breaker": {
            "enabled": true,
            "max_drawdown_pct": 0.15,
            "price_move_pct": 0.08,
            "price_move_candles": 3
        },
        "shared_state": {
            "persist_to_disk": true,
            "persist_path": "user_data/algo_system_state.json"
        },
        "observability": {
            "emit_module_metrics_rpc": true,
            "per_module_log_level": "INFO"
        }
    }
}
```

### 4.10 Observability

**FR-37:** Every module MUST have its own namespaced logger: `algo_system.{module_id}.{pair}`

**FR-38:** `MetricsCollector` MUST track per module: candles analyzed, signals generated, module switches triggered, suspension events, estimated P&L contribution

**FR-39:** Module suspension events MUST emit as RPC messages (delivered via existing Telegram/webhook integration)

---

## 5. Non-Functional Requirements

| ID | Category | Requirement |
|----|----------|-------------|
| NFR-1 | Performance | `populate_indicators()` across all active modules MUST complete within the timeframe window (no blocking) |
| NFR-2 | Reliability | Single module failure MUST NOT crash the bot or affect other modules |
| NFR-3 | Testability | Every module MUST be unit-testable without a running freqtrade instance using `MockModuleContext` |
| NFR-4 | Extensibility | Adding a new algorithm module MUST require zero changes to orchestrator code |
| NFR-5 | Safety | No live order MUST be placed without trade attribution (enter_tag prefix) |
| NFR-6 | Isolation | Module state (`GridState`, etc.) MUST NOT leak between pairs |
| NFR-7 | Memory | `SharedState` MUST support disk persistence to survive bot restarts |
| NFR-8 | Compatibility | System MUST work with freqtrade v2024.8-dev without patches |

---

## 6. Data Models

### 6.1 ModuleSignal
```python
@dataclass
class ModuleSignal:
    enter_long:  Optional[bool] = None
    enter_short: Optional[bool] = None
    exit_long:   Optional[bool] = None
    exit_short:  Optional[bool] = None
    entry_tag:   Optional[str]  = None   # MUST be prefixed "{module_id}:"
    exit_tag:    Optional[str]  = None
    confidence:  float = 1.0             # 0.0–1.0
    metadata:    Dict  = field(default_factory=dict)
```

### 6.2 RoutingDecision
```python
@dataclass
class RoutingDecision:
    authoritative_module_id: str
    confidence: float                  # 0.0–1.0
    rationale: str                     # human-readable, logged + RPC
    valid_until_candle: int            # TTL in candles
    fallback_module_id: Optional[str] = None
```

### 6.3 GridState
```python
@dataclass
class GridState:
    pair: str
    grid_levels: List[float]
    filled_levels: Set[float]
    upper_bound: float
    lower_bound: float
    grid_count: int
    initial_entry_price: Optional[float]
    created_at: datetime
    last_updated: datetime
```

### 6.4 AlgoLifecycleState (enum)
```python
class AlgoLifecycleState(str, Enum):
    INACTIVE   = "inactive"
    ACTIVE     = "active"
    DRAINING   = "draining"    # closing positions before switch
    SWITCHING  = "switching"   # new module taking over
    SUSPENDED  = "suspended"   # error threshold exceeded
```

---

## 7. Existing Data Sources

| Source | Library | Purpose in System |
|--------|---------|-------------------|
| Exchange OHLCV | `ccxt 4.3.85` | Candle data for all modules |
| Technical indicators | `pandas-ta 0.3.14b` | RSI, ATR, MACD, ADX, BB for reasoning layer |
| CoinGecko market data | `pycoingecko 3.1.0` | Market cap, dominance — available for reasoning features |
| FreqAI feature pipeline | `freqtrade/freqai/` | Phase 3 training data |
| EODHD (US equities) | `eodhd` (custom) | Separate equity screener — not integrated in Phase 1 |

### 7.1 Documentation References (Context7)

All library documentation is available via Context7 MCP — use these IDs when developing against these APIs:

| Library | Context7 ID | Snippets | Notes |
|---------|------------|----------|-------|
| EODHD Python Client | `/eodhistoricaldata/eodhd-apis-python-financial-library` | 52 | Official Python SDK — use for APIClient usage |
| EODHD Full API Docs | `/eodhistoricaldata/eodhd-claude-skills` | 2376 | Full endpoint coverage — screener, technicals, fundamentals |
| EODHD Financial APIs | `/eodhistoricaldata/eodhd-financial-apis` | 1353 | REST API reference — historical, live, economic data |
| PyCoinGecko | `/man-c/pycoingecko` | 77 | CoinGecko V3 wrapper — market cap, dominance, OHLCV |

**Usage:** When implementing any feature that touches these libraries, resolve docs with:
```
context7: /eodhistoricaldata/eodhd-claude-skills   # EODHD
context7: /man-c/pycoingecko                        # CoinGecko
```

---

## 8. Known Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Algorithm switch mid-position leaves orphaned trades | BLOCKER | AlgoLifecycleSM DRAINING state — old module keeps stoploss until all positions confirmed closed |
| `_cached_grouped_trades_per_pair` class-level mutable shared across strategy instances | CRITICAL | OrchestratorStrategy is the ONLY IStrategy; modules are NOT IStrategy subclasses — avoids shared state entirely |
| Grid module consumes all `max_open_trades` slots | CRITICAL | BudgetAllocator enforces per-module quotas |
| Over-allocation of capital across modules | CRITICAL | BudgetAllocator enforces `stake_allocation_pct` per module |
| Conflicting signals (long + short on same pair) | CRITICAL | SignalArbiter resolves via priority hierarchy before execution |
| FreqAI regime detection stale during volatile markets | MAJOR | CircuitBreaker runs independently of FreqAI on every candle; triggers on raw price move % |
| EODHD API key hardcoded in screener scripts | BLOCKER | Move to `os.environ.get('EODHD_API_KEY')` immediately; rotate existing key |

---

## 9. Phased Delivery Plan

### Phase 1 — MVP (Grid + Rule Reasoning + Backtest/Paper)

**Goal:** GridTrading module running in backtest and paper mode with rule-based reasoning.

**Deliverables:**

| # | File | Description |
|---|------|-------------|
| 1 | `base/ialgo_module.py` | IAlgoModule ABC, ModuleCapability, ModuleSignal, ModuleContext |
| 2 | `base/module_context.py` | Facades over DataProvider and Wallets |
| 3 | `orchestrator/shared_state.py` | In-memory SharedState with optional disk persistence |
| 4 | `orchestrator/module_registry.py` | Static registry (list-configured, no filesystem scan yet) |
| 5 | `orchestrator/signal_arbiter.py` | Priority-based conflict resolver |
| 6 | `orchestrator/budget_allocator.py` | Per-module stake + trade slot quotas |
| 7 | `orchestrator/algo_lifecycle.py` | INACTIVE→ACTIVE→DRAINING→SWITCHING→SUSPENDED SM |
| 8 | `orchestrator/orchestrator_strategy.py` | Full IStrategy; all callbacks delegate to modules |
| 9 | `reasoning/rule_based_reasoning.py` | ADX + BB width regime detection |
| 10 | `modules/grid_trading/grid_calculator.py` | Pure grid math, zero FT deps |
| 11 | `modules/grid_trading/grid_state.py` | GridState dataclass |
| 12 | `modules/grid_trading/grid_trading_module.py` | Full IAlgoModule implementation |
| 13 | `config/algo_config_schema.py` | TypedDict schema with validation |
| 14 | `tests/conftest.py` | MockModuleContext, MockDataProvider, MockWallets |
| 15 | `tests/test_grid_module.py` | Grid level crossing, position adjust, state tracking |
| 16 | `tests/test_orchestrator.py` | Signal routing, budget enforcement, suspension |
| 17 | `tests/harness/backtest_harness.py` | Programmatic per-module backtest runner |

**Acceptance Criteria:**
- [ ] `freqtrade backtesting --strategy OrchestratorStrategy` completes on real data
- [ ] `freqtrade trade --dry-run` runs 1+ hours without crash
- [ ] GridTrading places position adjustment orders visible in backtest results
- [ ] BudgetAllocator prevents over-allocation in backtest simulation
- [ ] All unit tests pass

---

### Phase 2 — Production Hardening + Observability

**Goal:** Production-grade module loading, logging, metrics, and error recovery.

**Deliverables:**
- `orchestrator/module_resolver.py` — IResolver-style filesystem discovery of IAlgoModule subclasses
- `observability/module_logger.py` — Namespaced per-module structured logger
- `observability/metrics_collector.py` — RPC metrics emission to Telegram
- Circuit breaker integration (price move + drawdown triggers)
- `SharedState` disk persistence and restart recovery
- Module auto-suspension with Telegram alert
- Extended test suite: module switching, suspension, circuit breaker

**Acceptance Criteria:**
- [ ] New module added by dropping file in `modules/` directory, zero orchestrator changes
- [ ] Telegram receives alert within 1 candle of module suspension
- [ ] Bot state survives restart (SharedState reload from disk)
- [ ] CircuitBreaker fires correctly in backtested crash scenarios

---

### Phase 3 — FreqAI Reasoning + HyperOpt Integration

**Goal:** ML-powered module routing and HyperOpt-able module parameters.

**Deliverables:**
- `reasoning/freqai_reasoning.py` — FreqAIReasoningEngine adapter
- `OrchestratorStrategy.set_freqai_targets()` — produces `&-grid_suitable` label
- `OrchestratorStrategy.feature_engineering_expand_all()` — module-aware features
- Per-module `feature_engineering()` hook (adds `__{module_id}__` prefixed columns)
- `GridTradingModule.describe_parameters()` — IntParameter/DecimalParameter for HyperOpt
- RL environment integration (`freqai/RL/BaseEnvironment`) per module
- Example config: `config_examples/config_algo_system_freqai.json`

**Acceptance Criteria:**
- [ ] FreqAI trains `&-grid_suitable` predictor successfully
- [ ] ReasoningEngine switches from RuleBased to FreqAI via config change only
- [ ] Grid parameters are HyperOpt-able: `grid_count`, `grid_range_pct`
- [ ] RL agent can be plugged in as an alternative reasoning engine

---

## 10. Integration Points with Freqtrade Core

| Freqtrade Mechanism | How LATS Uses It |
|---------------------|-----------------|
| `IStrategy.populate_indicators()` | Aggregates all module indicators, runs FreqAI if enabled |
| `IStrategy.populate_entry_trend()` | Queries reasoning layer, delegates to authoritative module |
| `IStrategy.adjust_trade_position()` | Core grid mechanic — all grid orders placed here |
| `IStrategy.confirm_trade_entry()` | Final validation gate before order placement |
| `IStrategy.order_filled()` | Broadcasts to all modules for state updates |
| `IStrategy.bot_loop_start()` | Reasoning layer market-wide observation |
| `IResolver` / `PathModifier` | Module discovery mirrors this exact pattern |
| `FreqaiDataDrawer.pair_dict` | SharedState mirrors this persistent in-memory pattern |
| `strategy_safe_wrapper` | Module call exception isolation mirrors this pattern |
| `RunMode` enum | Modules advertise capability per run mode |
| `enter_tag` column | Trade attribution — all tags prefixed `"{module_id}:"` |
| `RPCManager` | Metrics and suspension alerts emitted as custom RPC messages |
| `pycoingecko 3.1.0` | Available for reasoning layer market-wide features (Phase 2+) |
| `pandas-ta 0.3.14b` | ADX, BB width for RuleBasedReasoningEngine (Phase 1) |

---

## 11. Out of Scope (Current PRD)

- EODHD equity screener integration (separate research tool, stays in `research/`)
- Multi-exchange arbitrage
- Options or derivatives trading
- Portfolio rebalancing across spot + futures
- External signal providers (webhook-based signals)
- Web UI for module configuration (uses existing freqtrade UI)

---

## 12. Open Questions

| # | Question | Owner | Target |
|---|----------|-------|--------|
| Q1 | What is the initial capital budget for live testing? Affects `stake_allocation_pct` defaults | User | Before Phase 1 complete |
| Q2 | Which exchange for live trading? Affects `can_short` capability and leverage settings | User | Before Phase 1 complete |
| Q3 | Should CoinGecko data (market cap, dominance) feed the reasoning layer in Phase 2? | User | Phase 2 planning |
| Q4 | Phase 3: preference for supervised FreqAI or RL-based reasoning? Both are available | User | Phase 3 planning |
| Q5 | Should the equity screener (`eohd_screeener_max.py`) eventually generate crypto watchlists via CoinGecko? | User | Backlog |

---

## 13. Immediate Pre-Work (Before Phase 1 Starts)

1. **Rotate EODHD API key** — currently hardcoded in `eohd_screeener.py` and `eohd_screeener_max.py`
2. **Move screener files** to `research/equity_screener/` and add to `.gitignore`
3. **Fix `eohd_screeener.py`** — `NameError: api_token` crashes on first run (line 288)
4. **Add `research/` and `prd/` to `.gitignore`** if not committing them to upstream

# Implementation Plan: Layered Algorithmic Trading System (LATS)

**Branch**: `001-lats-algo-trading` | **Date**: 2026-03-16 | **Spec**: [spec.md](spec.md)

---

## Summary

Build a non-invasive extension layer on top of freqtrade that presents as a single `IStrategy` while internally orchestrating multiple pluggable algorithm modules through a reasoning layer. Phase 1 delivers `OrchestratorStrategy` + `GridTradingModule` + `RuleBasedReasoningEngine` working in backtest and paper trading modes with full capital isolation, signal arbitration, and circuit breaker safety. No freqtrade core files are modified.

---

## Technical Context

**Language/Version**: Python 3.11 (freqtrade supported range: 3.9–3.12)
**Primary Dependencies**:
- `freqtrade v2024.8-dev` — runtime engine (untouched)
- `talib` — ADX, Bollinger Bands for rule-based reasoning (`talib.abstract` as used in freqtrade templates)
- `technical` (qtpylib) — Bollinger Bands via `qtpylib.bollinger_bands()`
- `gymnasium` — RL environments (Phase 3, already in `requirements-freqai-rl.txt`)
- `stable-baselines3` — RL training (Phase 3, already in `requirements-freqai-rl.txt`)
- `python-dotenv` — API key loading
- `pycoingecko 3.1.0` — optional market data enrichment (Phase 2+)

**Storage**:
- `user_data/algo_system_state.json` — SharedState disk persistence (JSON + threading.Lock)
- freqtrade's SQLite `tradesv3.sqlite` — Trade model (read-only, no schema changes)

**Testing**: `pytest` (freqtrade's existing test runner), `MockModuleContext` (custom harness)

**Target Platform**: Linux (Ubuntu 5.15.0-171), single process, no sudo

**Project Type**: freqtrade strategy extension (plugin layer, not standalone service)

**Performance Goals**:
- `populate_indicators()` across all active modules completes within timeframe window
- Backtest of 6 months historical data completes in under 5 minutes
- Paper trading 24h without memory leak or crash

**Constraints**:
- Zero modifications to freqtrade core files
- No new database columns (use `enter_tag` prefix for attribution)
- No external state service (SQLite/JSON only)
- No sudo (rootless environment)

**Scale/Scope**: Single trader, 1 bot process, up to 20 pairs, 2–5 algorithm modules

---

## Constitution Check

*No project constitution exists yet. Proceeding with freqtrade community conventions as implicit constitution:*

| Rule | Status | Notes |
|------|--------|-------|
| Do not modify freqtrade core | PASS | All code in `user_data/strategies/algo_system/` |
| Single IStrategy loaded by bot | PASS | OrchestratorStrategy is the only IStrategy |
| Use existing freqtrade patterns | PASS | Mirrors IResolver, strategy_safe_wrapper, RPCManager patterns |
| No new DB schema | PASS | Attribution via enter_tag prefix only |
| Rootless (no sudo) | PASS | pip install only, user_data/ paths |
| Test without live exchange | PASS | MockModuleContext + backtest harness |

*Recommendation*: Run `/speckit.constitution` to formalize these as project rules.

---

## Project Structure

### Documentation (this feature)

```text
specs/001-lats-algo-trading/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 research findings
├── data-model.md        # Entity definitions and state diagrams
├── contracts/
│   ├── ialgo_module_contract.md       # IAlgoModule interface contract
│   └── reasoning_engine_contract.md   # IReasoningEngine interface contract
├── checklists/
│   └── requirements.md               # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks — not yet created)
```

### Source Code Layout

```text
user_data/strategies/algo_system/
├── __init__.py
│
├── orchestrator/
│   ├── __init__.py
│   ├── orchestrator_strategy.py    # IStrategy freqtrade loads
│   ├── module_registry.py          # discover + register IAlgoModule instances
│   ├── module_resolver.py          # IResolver-style dynamic class loader
│   ├── signal_arbiter.py           # conflict resolution + priority hierarchy
│   ├── budget_allocator.py         # per-module stake + trade slot quotas
│   ├── algo_lifecycle.py           # ModuleState SM + transition guards
│   └── shared_state.py             # cross-module KV store + disk persistence
│
├── base/
│   ├── __init__.py
│   ├── ialgo_module.py             # IAlgoModule ABC + ModuleCapability + ModuleSignal
│   └── module_context.py           # DataProviderProxy + WalletsProxy + ModuleContext
│
├── modules/
│   ├── __init__.py
│   └── grid_trading/
│       ├── __init__.py
│       ├── grid_trading_module.py  # IAlgoModule implementation
│       ├── grid_calculator.py      # pure grid math (no freqtrade deps)
│       └── grid_state.py           # GridState dataclass
│
├── reasoning/
│   ├── __init__.py
│   ├── reasoning_interface.py      # IReasoningEngine ABC + RoutingDecision
│   ├── rule_based_reasoning.py     # ADX + BB width regime detection (Phase 1)
│   └── freqai_reasoning.py         # FreqAI adapter (Phase 3)
│
├── safety/
│   ├── __init__.py
│   └── circuit_breaker.py          # CircuitBreaker + CircuitBreakerState
│
├── config/
│   ├── __init__.py
│   └── algo_config_schema.py       # TypedDict schema + validation at startup
│
├── observability/
│   ├── __init__.py
│   ├── module_logger.py            # namespaced per-module logger factory
│   └── metrics_collector.py        # dp.send_msg() adapter for RPC alerts
│
└── tests/
    ├── __init__.py
    ├── conftest.py                  # MockModuleContext, MockDataProvider, MockWallets
    ├── test_grid_module.py          # GridCalculator, GridState, level crossings
    ├── test_orchestrator.py         # signal routing, budget enforcement, suspension
    ├── test_reasoning.py            # RuleBasedReasoningEngine scoring
    ├── test_circuit_breaker.py      # threshold triggers, stoploss preservation
    └── harness/
        ├── backtest_harness.py      # programmatic freqtrade Backtesting runner
        └── mock_exchange.py         # exchange stub for isolated testing
```

**Structure Decision**: Single project, extension layer pattern. All new code under `user_data/strategies/algo_system/`. No separate packages, no new top-level directories in freqtrade root.

---

## Implementation Phases

### Phase 1 — MVP (Grid + Rule Reasoning + Backtest/Paper)

**Sequence** (dependency-ordered):

```
1. base/ialgo_module.py           ← no deps, defines the contract
2. base/module_context.py         ← depends on ialgo_module types
3. orchestrator/shared_state.py   ← no deps
4. safety/circuit_breaker.py      ← no deps
5. orchestrator/algo_lifecycle.py ← depends on ModuleState enum
6. orchestrator/budget_allocator.py ← depends on shared_state
7. orchestrator/signal_arbiter.py ← depends on ModuleSignal
8. reasoning/reasoning_interface.py ← depends on RoutingDecision
9. reasoning/rule_based_reasoning.py ← depends on reasoning_interface
10. modules/grid_trading/grid_calculator.py ← pure math, no deps
11. modules/grid_trading/grid_state.py ← depends on calculator types
12. modules/grid_trading/grid_trading_module.py ← depends on base + grid_state
13. config/algo_config_schema.py  ← depends on all module types
14. orchestrator/module_registry.py ← depends on base, config
15. orchestrator/orchestrator_strategy.py ← assembles everything
16. tests/conftest.py             ← MockModuleContext harness
17. tests/test_grid_module.py     ← grid unit tests
18. tests/test_orchestrator.py    ← integration tests
19. tests/harness/backtest_harness.py ← end-to-end backtest runner
```

**Parallelizable groups**:
- **Wave 1** (parallel): items 1–4 (foundational, zero interdependencies)
- **Wave 2** (parallel): items 5–9 (depend only on Wave 1)
- **Wave 3** (parallel): items 10–11 (pure grid math)
- **Wave 4** (sequential): items 12–15 (assembly, each depends on prior)
- **Wave 5** (parallel): items 16–19 (tests, all depend on Wave 4)

---

### Phase 2 — Production Hardening

- `orchestrator/module_resolver.py` — IResolver-style dynamic filesystem discovery
- `observability/module_logger.py` + `observability/metrics_collector.py`
- Module auto-suspension with RPC alert via `dp.send_msg()`
- SharedState disk persistence + restart recovery
- Extended test suite: switching, suspension, circuit breaker

---

### Phase 3 — FreqAI Reasoning + RL Environments

- `reasoning/freqai_reasoning.py` — FreqAI adapter for regime detection
- `OrchestratorStrategy.set_freqai_targets()` → `&-grid_suitable` label
- FreqAI RL environments: `Base3ActionRLEnv` / `Base4ActionRLEnv` (already in freqtrade via Gymnasium)
- Grid module HyperOpt parameters (`IntParameter`, `DecimalParameter`)
- *See RL environment comparison research (pending) for environment selection*

---

## Key Implementation Patterns (from Research)

### Exception Isolation (mirror `strategy_safe_wrapper`)
```python
def _safe_module_call(self, module, method, *args, default=None, **kwargs):
    try:
        return getattr(module, method)(*args, **kwargs)
    except Exception as e:
        self._record_module_failure(module.module_id)
        logger.exception(f"Module {module.module_id}.{method}: {e}")
        return default
```

### Plugin Discovery (mirror `IResolver._get_valid_object`)
```python
spec = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
classes = [
    obj for _, obj in inspect.getmembers(module, inspect.isclass)
    if issubclass(obj, IAlgoModule)
    and obj is not IAlgoModule
    and obj.__module__ == name
]
```

### RPC Alerts (via `DataProvider.send_msg`)
```python
# In OrchestratorStrategy:
self.dp.send_msg(f"[LATS] Module {module_id} SUSPENDED", always_send=True)
# Delivered to Telegram/webhook automatically by freqtrade's main loop
```

### Grid Level Crossing Detection (GridCalculator)
```python
def get_crossed_levels_down(from_price, to_price, levels):
    """Price moved DOWN through levels — buy triggers."""
    return [l for l in levels if to_price <= l < from_price]

def get_crossed_levels_up(from_price, to_price, levels, filled):
    """Price moved UP through filled levels — sell triggers."""
    return [l for l in filled if from_price < l <= to_price]
```

---

## Critical Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Grid orders mechanism | `adjust_trade_position()` only | Freqtrade-native, works in backtest and live |
| Trade attribution | `enter_tag` prefix `"{module_id}:"` | No DB schema changes, 255 char limit sufficient |
| Indicators library | `talib` + `qtpylib` (NOT pandas-ta) | Freqtrade templates use talib; consistent with existing codebase |
| State machine | `str Enum` + VALID_TRANSITIONS dict | Zero deps, mirrors freqtrade's own enums/state.py |
| RPC alerts | `dp.send_msg()` | Standard freqtrade channel, auto-routes to all RPC handlers |
| Exception isolation | Mirror `strategy_safe_wrapper` | Production-tested pattern, `supress_error=True` for non-critical |
| Capability flags | Class-level Enum vars | Mirrors `IPairList.supports_backtesting` pattern |
| RL environments | Gymnasium (Farama, actively maintained) + freqtrade's BaseEnvironment | Already in freqtrade RL stack |

---

## Open Items for Phase 3

- RL environment comparison: Gymnasium (freqtrade built-in) vs FinRL vs AnyTrading — *research agent running*
- Should LATS use freqtrade's `Base3ActionRLEnv` directly or subclass it for module routing?
- CoinGecko enrichment features for reasoning layer (Phase 2 scope)
- FreqAI training data labeling strategy for `&-grid_suitable`

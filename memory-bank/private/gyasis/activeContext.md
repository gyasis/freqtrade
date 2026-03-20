# Active Context

**Last Updated**: 2026-03-20 19:35:08

## Current Focus
feat: LATS Phase 1 — Layered Algorithmic Trading System

Implements the full LATS orchestration framework on top of freqtrade,
enabling pluggable algo modules, rule-based routing, grid trading, and
multi-asset execution abstraction (Phase 1 complete, 126 tests passing).

## Core Architecture

- **IAlgoModule ABC** (`base/ialgo_module.py`) — contract every trading
  module must satisfy; lifecycle hooks (initialize, tick, adjust_position,
  on_order_filled, reset_module_state, generate_entry_signal)
- **ModuleContext** (`base/module_context.py`) — immutable per-candle
  context passed to every module call (pair, df, trade, current_rate,
  reasoning_hints, recent_candles)
- **OrchestratorStrategy** (`orchestrator/orchestrator_strategy.py`) —
  freqtrade IStrategy that routes decisions through the LATS pipeline;
  caches routing decisions per pair across populate_* calls
- **AlgoLifecycleSM** (`orchestrator/algo_lifecycle.py`) — state machine
  (COLD → ACTIVE → DRAINING → INACTIVE); governs safe module transitions
- **ModuleRegistry** (`orchestrator/module_registry.py`) — discovers,
  validates, and manages active modules; integrates PairSelector for
  pair whitelisting per module
- **BudgetAllocator** (`orchestrator/budget_allocator.py`) — per-module
  stake budgets; guards against negative stakes and returns deep copies
- **PairSelector** (`orchestrator/pair_selector.py`) — ADX+BB width pair
  scoring with optional talib; warns once on import failure (no crash)
- **SharedState** (`orchestrator/shared_state.py`) — persistent JSON state
  store across bot restarts; namespace-keyed storage
- **SignalArbiter** (`orchestrator/signal_arbiter.py`) — resolves conflicts
  when multiple modules signal on the same pair

## GridTradingModule (modules/grid_trading/)

- Symmetric grid: upper/lower bounds, N evenly-spaced levels
- `GridState`: tracks filled/unfilled levels, serialises to/from dict
  including `out_of_range_candles` for Phase 2 breakout detection
- Critical bug fixes over initial draft:
  - **prev_rate tracking**: `_last_prices` dict instead of stale `trade.open_rate`
  - **No double record_open**: `record_open` called only in `order_filled`
  - **Optimistic mark_filled**: marks level filled before returning buy stake
  - **mark_unfilled on sell fills**: `on_order_filled` correctly handles both sides
  - **Negative stake bypass guard**: BudgetAllocator rejects `stake <= 0`
  - **Mutable budget reference fix**: `get_all_budgets` returns deep copies

## Reasoning Engine (reasoning/)

- `IReasoningEngine` interface with `score_module`, `get_routing_decision`,
  `record_trade_outcome` (Bayesian hook for Phase 2)
- `RuleBasedReasoningEngine`: ADX+BB width indicator scoring, TTL-cached
  routing decisions, `tick()` hook for time-based decay
- `EntryQualityEvaluator`: multi-factor entry filter (ADX, RSI, BB position,
  volume); integrated into GridTradingModule.generate_entry_signal

## Execution Abstraction (execution/)

- `IBrokerBackend` ABC — asset_class, get_ohlcv, place_order, get_balance,
  get_open_positions, get_symbols, is_paper_trading
- `FreqtradeBackend` — wraps freqtrade DataProvider + Wallets for crypto
- `MockBrokerBackend` — fully functional in-memory backend for tests

## Safety (safety/)

- `CircuitBreaker`: drawdown-based halt with configurable thresholds,
  cooldown periods, and recovery confirmation candles

## Observability (observability/)

- `MetricsCollector`: Telegram alerts for routing changes, circuit breaks,
  suspension events, and (Phase 2) grid status snapshots
- `ModuleLogger`: structured per-pair/per-module logging

## Test Suite (tests/) — 126 tests

- test_grid_module.py (49 tests) — grid init, crossings, fills, prev_rate
  tracking, order-filled side handling, entry quality gating
- test_orchestrator.py (38 tests) — budget allocator, pair selector, module
  registry, module discovery, lifecycle SM
- test_reasoning.py (21 tests) — scoring, routing, TTL, entry quality eval
- test_circuit_breaker.py (18 tests)
- test_broker_backend.py (16 tests) — IBrokerBackend contract, mock backend
- test_helpers.py + conftest.py — shared fixtures and OHLCV helpers

## Specs & Roadmap

- `specs/001-lats-algo-trading/` — Phase 1 spec, plan, contracts, tasks (50/50)
- `specs/002-lats-phase2/stories/` — Phase 2 (US-P2-01–06) and Phase 3
  (US-P3-01–05) user stories covering ATR grid sizing, Bayesian scoring,
  observability dashboard, breakout exits, IBrokerBackend, StockScreener,
  AlpacaBackend, OANDABackend, and morning_run.py workflow
- `prd/layered-algo-trading-system.md` — product requirements document

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

## Recent Changes
```
 .claude/AGENT_STATE.json                       |   7 +-
 .claude/activity_stream.md                     |  15 ++++
 .claude/session_snapshots/snapshot_latest.json |   2 +-
 .claude/system_bus.json                        |  10 +++
 memory-bank/private/gyasis/activeContext.md    | 106 ++++++++++++++++++++++---
 memory-bank/private/gyasis/progress.md         |   2 +-
 6 files changed, 126 insertions(+), 16 deletions(-)
```

## Modified Files
.claude/AGENT_STATE.json
.claude/activity_stream.md
.claude/session_snapshots/snapshot_latest.json
.claude/system_bus.json
memory-bank/private/gyasis/activeContext.md
memory-bank/private/gyasis/progress.md

## Next Actions
- Continue implementation
- Run tests
- Create checkpoint

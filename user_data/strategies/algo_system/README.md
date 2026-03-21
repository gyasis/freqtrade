# LATS — Layered Algorithmic Trading System

A modular, plugin-based strategy orchestration layer for freqtrade.

## Overview

LATS wraps freqtrade's `IStrategy` interface behind a clean plugin architecture.
A single `OrchestratorStrategy` class is loaded by freqtrade; it discovers and
delegates to `IAlgoModule` plugins at runtime.  Adding a new trading strategy
requires no changes to the orchestrator — drop a file, update config, done.

```
freqtrade
└── OrchestratorStrategy
    ├── RuleBasedReasoningEngine  — selects authoritative module per pair/candle
    ├── SignalArbiter             — resolves conflicting module signals
    ├── BudgetAllocator           — enforces per-module capital & trade-slot quotas
    ├── AlgoLifecycleSM           — per-pair FSM (INACTIVE→ACTIVE→DRAINING→SUSPENDED)
    ├── CircuitBreaker            — global safety halt on drawdown or price spike
    ├── PairSelector              — dynamic pair filtering per module
    └── [modules]
        └── GridTradingModule     — symmetric grid buy/sell via adjust_position
```

---

## How to Add a New Module (5 steps)

### 1. Create the module file

```
user_data/strategies/algo_system/modules/my_strategy/my_strategy_module.py
```

### 2. Implement `IAlgoModule`

```python
from algo_system.base.ialgo_module import IAlgoModule, ModuleCapability, ModuleSignal
from algo_system.base.module_context import ModuleContext
from pandas import DataFrame

class MyStrategyModule(IAlgoModule):
    module_id  = "my_strategy_v1"      # unique key — must match config
    version    = "1.0.0"

    supports_backtest = ModuleCapability.SUPPORTED
    supports_paper    = ModuleCapability.SUPPORTED
    supports_live     = ModuleCapability.UNSUPPORTED   # until tested
    supports_hyperopt = ModuleCapability.UNSUPPORTED
    supports_short    = ModuleCapability.UNSUPPORTED
    supports_position_adjust = ModuleCapability.UNSUPPORTED

    def initialize(self, ctx: ModuleContext) -> None:
        cfg = ctx.shared_state.get(self.module_id, "config") or {}
        # read your config here

    def populate_indicators(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> DataFrame:
        # add any indicator columns you need
        return df

    def generate_entry_signal(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal:
        # return ModuleSignal(enter_long=True, entry_tag="my_strategy_v1:entry", confidence=0.8)
        return ModuleSignal()

    def generate_exit_signal(self, df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal:
        return ModuleSignal()

    def adjust_position(self, trade, current_time, current_rate, current_profit,
                        min_stake, max_stake, ctx: ModuleContext):
        return None   # no position adjustment
```

### 3. Create the `__init__.py` for the sub-package

```
user_data/strategies/algo_system/modules/my_strategy/__init__.py  (empty)
```

### 4. Add the module to `user_data/config_algo_backtest.json`

```json
"algo_system": {
    "modules_path": "user_data/strategies/algo_system/modules",
    "active_modules": ["grid_trading_v1", "my_strategy_v1"],
    "default_module": "grid_trading_v1",
    "modules": {
        "my_strategy_v1": {
            "max_open_trades": 3,
            "stake_allocation_pct": 0.25
        }
    }
}
```

### 5. Start the bot

```bash
freqtrade trade --config user_data/config_algo_backtest.json \
                --strategy OrchestratorStrategy --dry-run
```

The bot will log `Module 'my_strategy_v1' v1.0.0 registered` on startup.

---

## Config Schema Reference

All LATS config lives under the `"algo_system"` key in your freqtrade config JSON.

### Top-level required keys

| Key | Type | Description |
|-----|------|-------------|
| `modules_path` | string | Path (relative to freqtrade root) where module packages live |
| `active_modules` | list[str] | Module IDs to load — order determines signal arbitration priority |
| `default_module` | string | Fallback module when reasoning engine has no opinion |

### `reasoning` block

Controls the `RuleBasedReasoningEngine`.

| Key | Default | Description |
|-----|---------|-------------|
| `engine` | `"rule_based"` | Engine type (only `"rule_based"` currently) |
| `adx_ranging_threshold` | `25.0` | ADX below this → ranging market → grid scores +0.6 |
| `bb_width_ranging_threshold` | `0.04` | BB width below this → grid scores +0.25 |
| `activation_threshold` | `0.6` | Minimum module score to issue a routing decision |
| `routing_ttl_candles` | `10` | How many candles a routing decision stays valid before re-evaluation |

### `circuit_breaker` block

| Key | Default | Description |
|-----|---------|-------------|
| `max_drawdown_pct` | `0.15` | Portfolio drawdown that trips the breaker (0–1) |
| `price_move_pct` | `0.08` | Price move within `price_move_candles` that trips the breaker |
| `price_move_candles` | `3` | Lookback window for price move check |
| `cooling_candles` | `10` | Candles to wait in COOLING state before returning to ARMED |

### `entry_quality` block

Controls `EntryQualityEvaluator` — gates grid initialization.

| Key | Default | Description |
|-----|---------|-------------|
| `entry_quality_threshold` | `0.0` | Score below this → defer entry. Set to `0.5` to enable gating |
| `momentum_lookback_candles` | `10` | Lookback for momentum calculation |
| `momentum_threshold` | `0.03` | Momentum above this (3%) → score penalty |
| `max_defer_candles` | `20` | Emit warning after deferring this many consecutive candles |

### `pair_selection` block

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Set to `false` to disable filtering (all whitelist pairs active) |
| `evaluation_interval_candles` | `24` | Re-evaluate pairs every N candles |
| `criteria` | `{}` | Per-module criteria overrides (see below) |

Per-module criteria example:
```json
"pair_selection": {
    "enabled": true,
    "criteria": {
        "grid_trading_v1": {
            "max_adx": 30.0,
            "min_bb_width": 0.005,
            "max_bb_width": 0.20
        }
    }
}
```

### Per-module budget in `modules`

```json
"modules": {
    "grid_trading_v1": {
        "max_open_trades": 5,
        "stake_allocation_pct": 0.5
    }
}
```

---

## Running Commands

```bash
# Backtest (6-month slice)
freqtrade backtesting \
    --config user_data/config_algo_backtest.json \
    --strategy OrchestratorStrategy \
    --timerange 20230601-20231201

# Paper trading
freqtrade trade \
    --config user_data/config_algo_backtest.json \
    --strategy OrchestratorStrategy \
    --dry-run

# Run unit tests
pytest user_data/strategies/algo_system/tests/ -v

# Run backtest harness
python user_data/strategies/algo_system/tests/harness/backtest_harness.py
```

---

## Architecture Notes

- **No freqtrade core files are modified** — all code lives under `user_data/strategies/algo_system/`
- **`enter_tag` format**: always `"{module_id}:{reason}"`, max 255 chars
- **`adjust_trade_position`** is the only mechanism for grid buy/sell orders (not `populate_entry_trend` after the initial entry)
- **`dp.send_msg()`** is the only alert channel — routes to Telegram/webhook automatically
- **talib is optional** — all talib calls are wrapped in try/except; system degrades gracefully without it (ADX/BB filtering disabled, all pairs qualify)
- **Thread safety** — `BudgetAllocator` and `SharedState` use `threading.Lock`; safe for freqtrade's callback threads

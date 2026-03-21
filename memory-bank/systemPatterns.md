# System Patterns: Freqtrade

## Architecture Overview

Freqtrade follows a modular, plugin-based architecture where the core bot orchestrates loosely-coupled subsystems.

```
freqtrade/
├── main.py                  # Entry point
├── worker.py                # Main loop / bot lifecycle manager
├── freqtradebot.py          # Core bot logic (trades, signals, orders)
├── strategy/                # Strategy interface + decorator helpers
├── exchange/                # Exchange connectors (CCXT-based)
├── optimize/                # Backtesting + Hyperopt
├── freqai/                  # ML prediction subsystem
├── rpc/                     # Telegram, REST API, WebUI
├── plugins/                 # Pairlist managers, protections
├── persistence/             # SQLite ORM (trades database)
├── data/                    # Data loading, history management
├── configuration/           # Config loading, validation
└── resolvers/               # Dynamic class loader (strategy, exchange, etc.)
```

## Core Design Patterns

### 1. Strategy-as-Code Pattern
All trading logic is encapsulated in user-defined Python classes that implement `IStrategy`. The bot calls well-defined interface methods:
- `populate_indicators()` - Add technical indicators to OHLCV DataFrame
- `populate_entry_trend()` - Define buy/entry signal conditions
- `populate_exit_trend()` - Define sell/exit signal conditions
- Optional: `custom_stoploss()`, `custom_exit()`, `confirm_trade_entry()`

### 2. Resolver Pattern (Dynamic Loading)
Freqtrade uses a resolver system to dynamically load:
- Strategy classes from `user_data/strategies/`
- FreqAI prediction models from `user_data/freqaimodels/`
- Custom Hyperopt loss functions from `user_data/hyperopts/`

This avoids hardcoding implementations and enables user-supplied plugins.

### 3. Exchange Adapter Pattern
All exchange interactions go through a unified `Exchange` class in `freqtrade/exchange/exchange.py`. Exchange-specific subclasses (binance.py, bybit.py, kraken.py, etc.) override only diverging behaviors. CCXT provides the underlying API layer.

### 4. Worker / Bot Loop Pattern
The `Worker` class manages the bot lifecycle:
- Handles startup/shutdown signals
- Runs the main trading loop
- Manages throttle timing (configurable tick interval)
- Delegates to `FreqtradeBot` for actual trading decisions

### 5. RPC / Observer Pattern
All external communication (Telegram, REST API, WebUI) goes through an RPC manager. The bot emits events (trade opened, trade closed, etc.) that RPC handlers consume. This decouples trading logic from notification/control logic.

### 6. DataHandler Pattern
Multiple data backends (JSON files, feather format) are supported through a common interface. Historical OHLCV data is cached locally under `user_data/data/`.

## FreqAI Subsystem Patterns

```
freqtrade/freqai/
├── freqai_interface.py      # Main FreqAI coordination class
├── data_kitchen.py          # Feature engineering, train/test splits
├── data_drawer.py           # Model persistence, prediction tracking
├── base_models/             # Base classes for all model types
├── prediction_models/       # Concrete model implementations
├── RL/                      # Reinforcement learning specific code
└── torch/                   # PyTorch model support
```

FreqAI trains on rolling windows and makes predictions that become signals in the strategy. The interface enforces a clean boundary: strategy code calls FreqAI through the interface, never accessing internals directly.

## Configuration Pattern

All bot behavior is driven by JSON config files. Key config sections:
- `exchange` - API credentials and exchange settings
- `stake_currency`, `stake_amount` - Capital management
- `strategy` - Which strategy class to load
- `freqai` - FreqAI model configuration
- `pairlists` - Which pairs to trade
- `telegram` / `api_server` - Control interfaces

## EODHD Screener Patterns (Custom Scripts)

The user's custom scripts follow distinct patterns worth documenting:

### Rate Limiting Pattern
```
RateLimiter class:
- Tracks API weight units (not raw request count)
- EODHD weights: RSI/ATR/MACD = 5 units each, default = 1
- 850 weight units per 60-second window (conservative vs 1000 limit)
- Deque-based sliding window to expire old requests
- Automatic 60-second pause when approaching limit
```

### Caching Pattern (eohd_screeener_max.py)
```
.cache/ directory (relative to script):
├── stock_prices_cache.csv      # Ticker prices with timestamps
├── failed_tickers.json         # Categorized failed tickers
└── logs/                       # Per-run screener logs

Cache validity: 24 hours
Failed ticker categories: invalid_period, delisted, other_errors
Strategy: Skip known-bad tickers on subsequent runs
```

### Retry Pattern
Uses `tenacity` library:
- 3 attempts maximum
- Exponential backoff: 4-10 seconds between retries
- Retries on: `requests.exceptions.RequestException`, `ValueError`
- Special handling: HTTP 429 (rate limit) triggers 60-second pause

### Chunked Processing Pattern
Yahoo Finance fetches in chunks of 50 tickers at a time to:
- Avoid hitting single-request limits
- Enable incremental cache saves
- Handle partial failures gracefully

## Key Technical Decisions in Core Freqtrade

| Decision | Rationale |
|----------|-----------|
| SQLite for trade persistence | Lightweight, no server required, sufficient for single-bot workloads |
| CCXT as exchange library | Unified API across 100+ exchanges, community maintained |
| Pandas DataFrames for OHLCV | Natural fit for time-series indicator computation |
| JSON config files | Human-readable, easy to version-control per deployment |
| Python 3.9+ minimum | Supports current type annotation syntax, broad compatibility |
| GPLv3 license | Ensures derivative works remain open source |

## Testing Patterns

- Framework: pytest
- Coverage tracked via Coveralls
- Test files mirror source structure under `tests/`
- Pre-commit hooks enforce code style (ruff formatter/linter)
- CI via GitHub Actions

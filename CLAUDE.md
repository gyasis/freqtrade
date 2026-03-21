# CLAUDE.md - Freqtrade Project Intelligence

This file captures project-specific patterns, preferences, and critical knowledge for working effectively in this codebase.

## Project Identity

- **Project:** freqtrade crypto trading bot (open source, version 2024.8-dev)
- **Branch:** develop (tracking upstream freqtrade/freqtrade develop)
- **User:** gyasis
- **Platform:** Linux (Ubuntu, kernel 5.15.0-171-generic)
- **Working dir:** `/home/gyasis/Documents/code/freqtrade`

## Memory Bank

All project memory lives in `/home/gyasis/Documents/code/freqtrade/memory-bank/`. Only the memory-bank-keeper agent may modify these files. Read all six files at session start to restore context:

1. `projectbrief.md` - What the project is
2. `productContext.md` - Why it exists, user's use case
3. `activeContext.md` - Current focus, recent changes, open questions
4. `systemPatterns.md` - Architecture and code patterns
5. `techContext.md` - Stack, dependencies, setup, constraints
6. `progress.md` - Status tracking with UTF-8 symbols

## Critical Project Knowledge

### Two Separate Workstreams

This repo contains TWO distinct bodies of work:

1. **Freqtrade core** - The open-source crypto trading bot (committed code)
2. **EODHD equity screener** - User's custom US stock analysis scripts (untracked files)

Do not confuse them. The screener scripts are personal research tooling, not part of freqtrade.

### Untracked Custom Files

| File | Purpose |
|------|---------|
| `eohd_screeener.py` | Basic EODHD stock screener |
| `eohd_screeener_max.py` | Advanced screener with caching, rate limiter, yfinance |
| `symbols.json` | 10,038 US tickers from SEC EDGAR (745KB) |
| `yfinance_debug.log` | Debug log, not critical |
| `deep_research.db` | SQLite DB (purpose unclear) |

### Security Warning

Both screener scripts contain a hardcoded EODHD API token: `6751a7dbc0dc70.26421855`

**Never commit these files without first moving the token to an environment variable or `.env` file.**

### Known Bug in Screener

`eohd_screeener_max.py` line 634: `analyze_stocks()` calls `RateLimiter(max_tokens=850, ...)` but `RateLimiter.__init__` takes `max_requests` not `max_tokens`. This will cause a `TypeError` if `analyze_stocks()` is called directly.

## Freqtrade Architecture Quick Reference

### Strategy Development Pattern
Strategies live in `user_data/strategies/` and inherit from `IStrategy`:
```python
from freqtrade.strategy import IStrategy
class MyStrategy(IStrategy):
    def populate_indicators(self, dataframe, metadata): ...
    def populate_entry_trend(self, dataframe, metadata): ...
    def populate_exit_trend(self, dataframe, metadata): ...
```

### Running Commands
```bash
# Trade
freqtrade trade --config user_data/config.json --strategy StrategyName

# Backtest
freqtrade backtesting --config user_data/config.json --strategy StrategyName

# Download data
freqtrade download-data --config user_data/config.json --pairs BTC/USDT ETH/USDT

# Hyperopt
freqtrade hyperopt --config user_data/config.json --strategy StrategyName --epochs 100
```

### FreqAI Notes
- Requires exchanges with historical OHLCV data (enforced since commit `01b7ad4a`)
- Models go in `user_data/freqaimodels/`
- Config section: `"freqai": {...}` in config JSON

## EODHD Screener Quick Reference

### Running the Advanced Screener
```bash
cd /home/gyasis/Documents/code/freqtrade
python eohd_screeener_max.py
```
Requires: `pip install eodhd yfinance tqdm tenacity requests`

### Cache Location
`.cache/` directory relative to script execution path:
- `stock_prices_cache.csv` - Price cache (24h validity)
- `failed_tickers.json` - Tracks problematic tickers
- `logs/` - Per-run screener logs
- `analysis_results_YYYYMMDD_HHMMSS_filter_MIN-MAX.csv` - Results

### API Weight Budget
EODHD Technical API: 850 weight units/minute (RSI/ATR/MACD = 5 each)

## Development Preferences (Observed)

- Uses pandas DataFrames throughout for data manipulation
- Prefers chunked processing with progress bars (tqdm)
- Implements caching aggressively to avoid redundant API calls
- Uses tenacity for production-grade retry logic
- Logs to both file and console (logging module)
- Script-style organization (Jupyter-compatible `# %%` cell markers)

## File Organization Recommendations

The root directory is getting cluttered. Recommended structure for custom work:
```
research/
├── equity_screener/
│   ├── eohd_screener_basic.py
│   ├── eohd_screener_advanced.py
│   ├── symbols.json
│   └── .env.example          # API_TOKEN=your_token_here
└── notebooks/
```

## Do Not

- Do not commit `symbols.json` without explicit user decision
- Do not commit screener scripts with hardcoded API tokens
- Do not modify files in `memory-bank/` (only memory-bank-keeper agent)
- Do not run `docker system prune` or other destructive Docker commands
- Do not use `sudo`
- Do not use `gpt-4` model - use `gpt-4.1-mini` instead

## Project Timeline Notes

- Last known upstream commits: 2024-08-23
- Memory bank initialized: 2026-03-15
- Gap between last commit and session: ~19 months (repo may not reflect latest upstream)

## Active Technologies
- Python 3.11 (freqtrade supported range: 3.9–3.12) (001-lats-algo-trading)

## Recent Changes
- 001-lats-algo-trading: Added Python 3.11 (freqtrade supported range: 3.9–3.12)

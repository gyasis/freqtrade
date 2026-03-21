# Progress: Freqtrade

## Overall Project Status

This is a fork/local installation of the upstream freqtrade open-source project. The user (gyasis) maintains their own develop branch while also building custom tooling on top of it.

**Core Platform Status:** :white_check_mark: Stable - upstream freqtrade 2024.8-dev

**Custom Tooling Status:** :hourglass_flowing_sand: In Progress - EODHD equity screener under active development

---

## Freqtrade Core Components

### Production-Ready (Upstream)

| Component | Status | Notes |
|-----------|--------|-------|
| Exchange connectivity (CCXT) | :white_check_mark: | Supports Binance, Bybit, Kraken, Gate.io, OKX, BingX, HTX, Bitmart |
| Strategy interface | :white_check_mark: | IStrategy with full indicator/signal/exit API |
| Backtesting engine | :white_check_mark: | Full OHLCV replay with parameter optimization |
| Hyperopt | :white_check_mark: | Bayesian optimization of strategy parameters |
| FreqAI subsystem | :white_check_mark: | ML-based adaptive prediction (scikit-learn, torch, RL) |
| Telegram control | :white_check_mark: | Full remote management |
| REST API + WebUI | :white_check_mark: | Built-in web interface |
| Trade persistence | :white_check_mark: | SQLite via SQLAlchemy |
| Edge position sizing | :white_check_mark: | Win rate / risk-reward calculation |
| Docker deployment | :white_check_mark: | docker-compose.yml provided |

### Recent Changes (2024-08-23 and prior)

| Commit | Status | Description |
|--------|--------|-------------|
| Improve typing | :white_check_mark: | Type annotation improvements throughout codebase |
| FreqAI exchange history check | :white_check_mark: | Prevents startup on exchanges without sufficient history |
| Binance leverage tiers update | :white_check_mark: | Updated leverage tier data for Binance |
| Pre-commit hooks update | :white_check_mark: | Development tooling maintenance |
| Untradeable pairs clarification | :white_check_mark: | Documentation and code clarity for market handling |
| WebSocket wait time increase | :white_check_mark: | Stability fix for WS connections |
| CCXT update to 4.3.85 | :white_check_mark: | Exchange library kept current |
| Tables pinned for Python 3.9 | :white_check_mark: | Compatibility fix |

---

## Custom EODHD Equity Screener

### eohd_screeener.py (Basic Screener)

| Feature | Status | Notes |
|---------|--------|-------|
| Market cap screener | :white_check_mark: | Top US stocks by market cap |
| Multi-filter screener | :white_check_mark: | Sector + market cap combined filters |
| 200-day new low signal | :white_check_mark: | EODHD signal filter working |
| Wall Street high signal | :white_check_mark: | Financial sector filter working |
| DataFrame output | :white_check_mark: | Clean output with relevant columns |
| API token security | :x: | Token hardcoded in script (should use env var) |
| Error handling | :warning: | Minimal - no retry or rate limit handling |
| Script organization | :warning: | Multiple disconnected code cells, not a clean module |

### eohd_screeener_max.py (Advanced Screener)

| Feature | Status | Notes |
|---------|--------|-------|
| Ticker loading from symbols.json | :white_check_mark: | Loads all 10,038 tickers |
| yfinance price fetching | :white_check_mark: | Chunked download (50 at a time) |
| 24-hour cache system | :white_check_mark: | Prevents redundant API calls |
| Failed ticker tracking | :white_check_mark: | Persists delisted/invalid tickers |
| Price range filter ($5-$45) | :white_check_mark: | Configurable via interactive menu |
| Rate limiter (EODHD) | :white_check_mark: | 850 weight units/min with countdown |
| Retry with backoff (tenacity) | :white_check_mark: | 3 attempts, 4-10s exponential backoff |
| RSI(14) via EODHD | :white_check_mark: | Technical indicator fetching working |
| ATR(14) via EODHD | :white_check_mark: | Technical indicator fetching working |
| MACD(12/26/9) via EODHD | :white_check_mark: | Technical indicator fetching working |
| Interactive filter menu | :white_check_mark: | Price filter, random sample, view saved |
| Results saved to CSV | :white_check_mark: | Timestamped files in .cache/ |
| Analysis display | :white_check_mark: | RSI/MACD/ATR breakdown with buy/sell signals |
| Combined signal filter | :white_check_mark: | RSI < 40 + bullish MACD histogram |
| API token security | :x: | Token hardcoded in multiple places |
| fetch_technical_data vs get_technical_indicator | :warning: | Two similar functions with different signatures exist |
| analyze_stocks() function | :warning: | References undefined `max_tokens` param in RateLimiter |
| Module structure | :warning: | Long script with mixed top-level execution and functions |

### symbols.json

| Aspect | Status | Notes |
|--------|--------|-------|
| Data completeness | :white_check_mark: | 10,038 US company tickers |
| Data source | :white_check_mark: | SEC EDGAR (authoritative) |
| File size | :warning: | 745KB - large for git tracking |
| .gitignore entry | :white_large_square: | Not yet decided |

---

## Housekeeping / Organization

| Task | Status | Priority |
|------|--------|---------|
| Move screener scripts to subdirectory | :white_large_square: | Medium |
| Externalize EODHD API token to .env | :white_large_square: | High (security) |
| Add .gitignore entries for custom files | :white_large_square: | Medium |
| Clean up deep_research.db (clarify purpose) | :white_large_square: | Low |
| Address .jython_cache/ in root | :white_large_square: | Low |
| Document active freqtrade strategies (if any) | :white_large_square: | High |
| Review user_data/ for any work in progress | :white_large_square: | High |

---

## What Is Known To Work

- Freqtrade core bot: full feature set as of 2024.8-dev
- EODHD screener: basic and advanced versions functionally working
- yfinance chunked downloads with caching
- EODHD technical indicator API calls (RSI, ATR, MACD)
- Rate limiting respecting EODHD's weight-based system
- symbols.json as ticker universe input

## What Needs Attention

1. **Security:** EODHD API token is hardcoded in two scripts
2. **Code quality:** `eohd_screeener_max.py` has a bug in `analyze_stocks()` - uses `max_tokens` parameter that doesn't exist in `RateLimiter` (it uses `max_requests`)
3. **Organization:** Custom scripts mixed into freqtrade root directory
4. **Documentation:** No docstrings or README for the custom screener scripts
5. **Testing:** No tests for the custom screener code

## Milestones

- :white_check_mark: Freqtrade 2024.8-dev deployed on develop branch
- :white_check_mark: EODHD basic screener functional
- :white_check_mark: EODHD advanced screener with caching + rate limiting functional
- :white_large_square: Custom freqtrade strategy development (unknown status)
- :white_large_square: FreqAI model in use (unknown status)
- :white_large_square: Live/dry-run trading configured (unknown status)

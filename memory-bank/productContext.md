# Product Context: Freqtrade

## Why This Project Exists

Freqtrade exists to democratize algorithmic trading by providing a professional-grade, open-source trading bot that individual traders can configure, backtest, and deploy without building infrastructure from scratch. It abstracts exchange connectivity, order management, position tracking, and risk management so users can focus on strategy development.

## Problems It Solves

### For Crypto Traders
- Eliminates the need to manually monitor markets 24/7
- Provides rigorous backtesting before risking real capital
- Enforces consistent strategy execution without emotional interference
- Handles exchange API complexity (rate limits, order types, error handling)

### For Strategy Researchers
- FreqAI enables ML model training directly on market data
- Hyperopt allows parameter optimization across large search spaces
- Built-in data management, plotting, and performance reporting

### For the User (gyasis)
The user operates freqtrade as both a crypto trading platform AND as a research hub for US equity market analysis. Evidence from custom scripts suggests a parallel research workflow:

1. **Crypto Trading:** Using freqtrade's core machinery for exchange-connected trading
2. **US Equity Screening:** Using EODHD API + yfinance to screen 10,038+ US stocks by:
   - Market capitalization filters
   - Price range filters ($5 - $45 default range)
   - Technical indicator signals (RSI, ATR, MACD)
   - Sector/industry classification
   - NYSE/NASDAQ 52-week high/low signals

## User's Research Workflow (EODHD Screener)

The custom screener scripts reveal a systematic approach to US equity research:

### eohd_screeener.py (Basic Version)
- Connects to EODHD API with a personal API token
- Screens US exchange stocks by market cap (top large caps)
- Applies signal filters: 200-day new lows, Wall Street high signals
- Generates filtered DataFrames for analysis

### eohd_screeener_max.py (Advanced Version)
- Loads 10,038 US tickers from `symbols.json` (SEC/EDGAR sourced)
- First-pass price filtering via Yahoo Finance (yfinance)
- Smart caching system: 24-hour cache validity, incremental updates
- Tracks failed/delisted tickers to avoid re-processing
- Rate limiter (850 API weight units/minute respecting EODHD's weighting scheme)
- Retry logic with exponential backoff (tenacity library)
- Technical indicator fetching: RSI(14), ATR(14), MACD(12/26/9)
- Interactive CLI menu for price range filtering and symbol selection
- Results saved to timestamped CSV files in `.cache/` directory
- Combined buy signals: RSI < 40 + bullish MACD histogram

### symbols.json
- 10,038 entries, each with: `cik_str`, `ticker`, `title`
- Sourced from SEC EDGAR company list
- Covers all publicly registered US companies including major ones (AAPL, MSFT, NVDA, AMZN, GOOGL)

## Technical Philosophy

Freqtrade follows these design principles:
- **Strategy-as-code:** All trading logic lives in Python classes implementing a defined interface
- **Separation of concerns:** Strategy, exchange, persistence, and risk management are decoupled
- **Test-first:** Comprehensive test suite (pytest) covering all major components
- **Config-driven:** JSON configuration files control all bot behavior
- **Plugin architecture:** Resolvers dynamically load strategies, exchange adapters, and FreqAI models

## Relationship to FreqAI

FreqAI is Freqtrade's integrated ML subsystem that:
- Trains prediction models on rolling windows of market data
- Retrains continuously as new data arrives (adaptive)
- Supports scikit-learn, PyTorch, and reinforcement learning backends
- Requires exchanges with sufficient historical data (recent commit: `01b7ad4a` prevents FreqAI startup on exchanges without history)

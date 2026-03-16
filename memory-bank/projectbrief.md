# Project Brief: Freqtrade

## Project Identity

**Name:** freqtrade
**Version:** 2024.8-dev
**License:** GPLv3
**Repository Branch:** develop
**Language:** Python 3.9+

## What This Project Is

Freqtrade is a free and open-source cryptocurrency trading bot written in Python. It is a mature, production-grade project that supports automated trading across all major crypto exchanges. The bot is controlled via Telegram or a built-in Web UI.

## Core Capabilities

- Automated crypto trading on spot and futures markets
- Backtesting and strategy simulation against historical data
- Strategy optimization via machine learning (Hyperopt)
- Adaptive prediction modeling via FreqAI (ML-based adaptive trading)
- Edge position sizing (win rate, risk/reward, optimal stoploss calculation)
- Dynamic and static pair whitelisting/blacklisting
- Dry-run mode for safe testing without real funds
- REST API and Telegram/WebUI control interfaces

## Supported Exchanges

**Fully Supported:**
- Binance (spot + futures)
- Bitmart, BingX, Gate.io, HTX, Kraken, OKX
- Bybit (futures)

**Community Tested:** Bitvavo, Kucoin

## Local User Customization Layer

The user (gyasis) is running this freqtrade installation and has added custom scripts for US equity stock screening using the EODHD API:

- `eohd_screeener.py` - Basic EODHD stock screener (market cap, technical indicators)
- `eohd_screeener_max.py` - Advanced screener with yfinance integration, caching, interactive menu, and rate limiting
- `symbols.json` - Large dataset of 10,038 US stock tickers (CIK, ticker, company name) sourced from SEC/EDGAR

These scripts are NOT part of freqtrade itself; they represent personal research/analysis tooling built alongside the trading bot.

## Project Goals

1. Operate freqtrade as a crypto trading infrastructure platform
2. Develop and backtest custom trading strategies
3. Leverage FreqAI for ML-driven strategy development
4. Use EODHD + yfinance tooling for US equity stock screening as a parallel research track
5. Maintain the codebase on the develop branch tracking upstream freqtrade releases

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `freqtrade/` | Core bot package |
| `user_data/` | User strategies, data, backtest results |
| `freqtrade/freqai/` | FreqAI ML subsystem |
| `freqtrade/exchange/` | Exchange connectors |
| `freqtrade/optimize/` | Backtesting and Hyperopt |
| `freqtrade/strategy/` | Strategy interface and helpers |
| `config_examples/` | Example config files |
| `tests/` | Full test suite |

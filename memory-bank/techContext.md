# Tech Context: Freqtrade

## Runtime Requirements

| Requirement | Specification |
|-------------|---------------|
| Python | 3.9, 3.10, 3.11, 3.12 supported |
| OS | Linux, macOS, Windows |
| Database | SQLite (built-in, no server) |
| Build system | setuptools >= 64.0.0 + wheel |

## Core Dependencies (requirements.txt)

Key production dependencies include:
- `ccxt` - Crypto exchange connectivity (updated frequently, e.g., 4.3.85)
- `pandas` - OHLCV data manipulation and indicator computation
- `numpy` - Numerical operations
- `sqlalchemy` - ORM for trade persistence
- `aiohttp` - Async HTTP for exchange API calls
- `python-telegram-bot` - Telegram control interface
- `tables` (PyTables / HDF5) - Data storage (pinned for Python 3.9 compat)
- `ruff` - Linting and formatting (replaces black/flake8)

## Optional Dependency Groups

| Group | Requirements File | Purpose |
|-------|-----------------|---------|
| FreqAI | requirements-freqai.txt | ML model backends (scikit-learn, lightgbm, etc.) |
| FreqAI RL | requirements-freqai-rl.txt | Reinforcement learning (stable-baselines3) |
| Hyperopt | requirements-hyperopt.txt | Bayesian optimization (optuna or hyperopt) |
| Plotting | requirements-plot.txt | Chart generation (plotly) |
| Dev | requirements-dev.txt | pytest, pre-commit, coverage tools |

## Custom Script Dependencies (User's EODHD Screener)

These are NOT in freqtrade's requirements and must be installed separately:

| Library | Purpose |
|---------|---------|
| `eodhd` | EODHD API Python client |
| `yfinance` | Yahoo Finance price data |
| `tqdm` | Progress bar display |
| `tenacity` | Retry logic with backoff |
| `requests` | HTTP calls to EODHD technical API |
| `pandas` | Data manipulation (shared with freqtrade) |

Install custom deps: `pip install eodhd yfinance tqdm tenacity requests`

## External API Keys Required

| Service | Used In | Notes |
|---------|---------|-------|
| EODHD API | eohd_screeener.py, eohd_screeener_max.py | Token currently hardcoded in scripts - should be moved to env var |
| Exchange API (varies) | freqtrade core | Set in freqtrade config JSON |
| Yahoo Finance | eohd_screeener_max.py | No key required (public API via yfinance) |

### EODHD API Rate Limits
- 1000 API weight units per minute (scripts use 850 as safe threshold)
- Technical indicators (RSI, ATR, MACD): 5 weight units each
- Fundamental/Options data: 10 weight units each
- Standard price data: 1 weight unit

## Development Setup

### Installation (Full)
```bash
# Clone and install
git clone https://github.com/freqtrade/freqtrade.git
cd freqtrade
pip install -e .[all]
# or use setup.sh
bash setup.sh
```

### Running the Bot
```bash
freqtrade trade --config user_data/config.json --strategy MyStrategy
```

### Backtesting
```bash
freqtrade backtesting --config user_data/config.json --strategy MyStrategy \
  --timerange 20240101-20240801
```

### Hyperopt
```bash
freqtrade hyperopt --config user_data/config.json --strategy MyStrategy \
  --hyperopt-loss SharpeHyperOptLoss --epochs 500
```

## Docker Support

`docker-compose.yml` and `Dockerfile` provided for containerized deployment. Docker Compose v2 syntax (`docker compose` not `docker-compose`).

## Code Quality Tools

| Tool | Config Location | Purpose |
|------|----------------|---------|
| ruff | pyproject.toml | Linting + formatting (replaces black+flake8) |
| pre-commit | .pre-commit-config.yaml | Hook runner |
| pytest | pyproject.toml | Test runner |
| coveralls | CI integration | Coverage reporting |

## File Structure Conventions

### User Data Directory (`user_data/`)
```
user_data/
├── data/              # Downloaded OHLCV data (exchange/pair/timeframe)
├── strategies/        # Custom strategy files (*.py)
├── hyperopts/         # Custom hyperopt loss functions
├── freqaimodels/      # Custom FreqAI model classes
├── backtest_results/  # Saved backtest output
├── notebooks/         # Jupyter notebooks for analysis
└── logs/              # Bot runtime logs
```

### Root-Level Custom Files (User-Added)
```
/home/gyasis/Documents/code/freqtrade/
├── eohd_screeener.py          # Basic EODHD screener (untracked)
├── eohd_screeener_max.py      # Advanced EODHD screener (untracked)
├── symbols.json               # 10,038 US stock tickers (untracked, 745KB)
├── yfinance_debug.log         # Debug log from yfinance operations
├── deep_research.db           # SQLite database (purpose TBD)
└── logs/                      # Log directory (shared with screener scripts)
```

## Environment Notes

- Platform: Linux (Ubuntu, kernel 5.15.0-171-generic)
- Shell: bash/zsh
- No sudo privileges (rootless environment)
- Working directory: `/home/gyasis/Documents/code/freqtrade`

## Known Technical Constraints

1. `tables` library pinned for Python 3.9 compatibility (commit `ce66fbb59`)
2. FreqAI requires exchanges that provide historical OHLCV data (enforced since `01b7ad4a`)
3. EODHD screener scripts have hardcoded API token - security risk if committed
4. `symbols.json` at 745KB should not be committed without explicit decision
5. The `.jython_cache/` directory in root is untracked (Jython artifact, unusual in Python env)

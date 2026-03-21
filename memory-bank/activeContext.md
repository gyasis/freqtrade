# Active Context: Freqtrade

## Current Session Date
2026-03-15 (memory bank initialized)

## Current Branch
`develop` - tracking upstream freqtrade develop branch

## Recent Upstream Activity (Last Known Commits as of 2024-08-23)

| Commit | Type | Description |
|--------|------|-------------|
| `e879275` | chore | Improve typing across codebase |
| `01b7ad4` | feat | Prevent FreqAI startup on exchanges without history |
| `235d387` | merge | Update Binance leverage tiers |
| `fd30edf` | chore | Update pre-commit hooks |
| `0a2be14` | merge | Froggleston PR (untradeable pairs clarity) |
| `33614d8` | docs | Improve wording for untradeable pairs |
| `4a62199` | docs | Add clarification for untradeable pairs vs markets |

Note: The most recent commits are from 2024-08-23. As of the current session date (2026-03-15), the repository appears to be significantly behind the current date — either this is a pinned snapshot or the repo has not been updated recently.

## Untracked Custom Files (User's Work)

These files exist in the project root but are NOT committed to git:

### :warning: eohd_screeener.py
- Basic EODHD stock market screener
- Uses `eodhd` Python library APIClient
- Demonstrates: market cap screening, signal filters (200d new low, Wall Street high)
- Contains an API token hardcoded: `6751a7dbc0dc70.26421855`
- Status: Development/experimental script, not integrated with freqtrade

### :warning: eohd_screeener_max.py
- Advanced screener extending the basic version
- New capabilities: yfinance integration, 24-hour caching, interactive menu, tenacity retry
- Loads tickers from `symbols.json`
- Price filter: $5 - $45 range (configurable interactively)
- Technical signals: RSI(14), ATR(14), MACD(12/26/9) via EODHD technical API
- Rate limiter: 850 API weight units/minute window
- Cache directory: `.cache/` (relative to script location)
- Status: More production-ready but still standalone; contains same hardcoded API token

### :warning: symbols.json
- 10,038 US stock tickers with CIK numbers and company names
- Sourced from SEC EDGAR public company filings database
- Used as input for `eohd_screeener_max.py`
- Large file (745KB) - not practical to commit without .gitignore consideration

## Immediate Security Note

Both screener scripts contain a hardcoded EODHD API token:
```
6751a7dbc0dc70.26421855
```
This token appears in plaintext in multiple locations within both files. This is a security concern if the files are ever committed to a public repository.

## Current Focus Areas

Based on the evidence:

1. **US Equity Research Pipeline** - The user is actively building a stock screening system using:
   - EODHD API for fundamental data and technical indicators
   - yfinance as a first-pass price data source
   - SEC EDGAR ticker list as universe
   - RSI/ATR/MACD as screening signals

2. **FreqAI Development** - Recent upstream commits suggest FreqAI is an active focus area (exchange history validation added)

3. **Codebase Maintenance** - Upstream typing improvements and pre-commit hook updates indicate active development hygiene

## Pending Decisions / Open Questions

- :white_large_square: Should the EODHD screener scripts be moved to a dedicated subdirectory (e.g., `scripts/equity_research/` or `research/`)?
- :white_large_square: Should `symbols.json` be added to `.gitignore` or committed as a data asset?
- :white_large_square: Is the intent to eventually integrate EODHD equity signals with freqtrade strategies?
- :white_large_square: Are there custom freqtrade strategies under development in `user_data/`?
- :white_large_square: Is FreqAI being used with any specific model backends?
- :white_large_square: What exchange(s) is the user actively trading on?

## Next Logical Steps (Inferred)

1. Move custom scripts to a proper subdirectory to keep root clean
2. Externalize API token to environment variable or config file
3. Consider `.gitignore` entry for `symbols.json` and `.cache/`
4. Review `user_data/` for any active strategies to document
5. Consider wrapping EODHD screener as a proper module with entrypoint

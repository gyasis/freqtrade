# US-P3-05 — morning_run.py: Human-in-the-Loop Daily Workflow

**Phase:** 3
**Priority:** P2
**Depends on:** US-P3-01, US-P3-02, US-P3-03, US-P3-04

---

## Problem

Running LATS across multiple asset classes requires a sequence of manual steps:

1. Run `eohd_screeener_max.py` → read CSV → choose stocks
2. Run freqtrade pair selector → read log output → choose crypto pairs
3. Manually run a backtesting command for each candidate symbol
4. Read separate backtest output files and compare results
5. Edit config files to set active pairs
6. Start freqtrade, AlpacaBackend, and OANDABackend in separate terminals

This is error-prone (easy to start trading a symbol that failed backtest), slow (10–15
minutes of manual work each morning), and opaque (decisions aren't recorded anywhere).

The workflow also mixes human judgment with mechanical steps: the human should approve
*which* symbols make it to trading, not manually execute every filter step.

---

## Goal

A single interactive CLI script, `scripts/morning_run.py`, that walks the operator
through the full morning setup. It handles all the mechanical work (screening, backtesting,
config writing, backend startup) while presenting results clearly and waiting for human
approval at each decision point.

Design philosophy:
- **Human stays in the loop**: every transition between stages requires explicit approval
  (unless `--auto` is set)
- **Transparent**: the script prints exactly what it is doing and why at each step
- **Recoverable**: state is written to disk after each stage; `--resume` restarts from
  the last completed stage
- **Auditable**: every decision is logged with timestamp to a dated log file
- **Not a black box**: `--dry-run` shows the full sequence without touching real APIs

---

## Invocation

```bash
# Standard interactive run
python scripts/morning_run.py

# Quick mode — uses yesterday's symbol picks as defaults, just confirm each
python scripts/morning_run.py --quick

# Automated mode — no prompts, full pipeline (institutional/CI use)
python scripts/morning_run.py --auto

# Dry run — simulates every step, no real API calls, no bot start
python scripts/morning_run.py --dry-run

# Resume from last completed stage (after crash or early exit)
python scripts/morning_run.py --resume

# Combine flags
python scripts/morning_run.py --quick --dry-run
```

---

## Flow Specification

### Stage 0: Preflight

Before any work begins, the script checks:
- Required environment variables are set (`EODHD_API_KEY`, `ALPACA_API_KEY`,
  `ALPACA_SECRET_KEY`, `OANDA_API_KEY`, `OANDA_ACCOUNT_ID`)
- `user_data/morning_run_state.json` is accessible (creates it if absent)
- LATS config file is present at `user_data/config.json`

Missing env vars are reported with instructions; missing optional backends are noted
but do not halt execution (the run proceeds with available backends only).

---

### Stage 1: Select Markets

```
=======================================================================
  LATS Morning Run — 2026-03-20 07:30 UTC
=======================================================================

STAGE 1: SELECT MARKETS
Which markets today?
  [1] Crypto only   (FreqtradeBackend)
  [2] Stocks only   (AlpacaBackend — paper)
  [3] Forex only    (OANDABackend — practice)
  [4] All markets   (default)
  [5] Custom...

Select [1-5] or press Enter for default: _
```

In `--quick` mode, the previous day's selection is shown as the default. In `--auto`
mode, "All markets" is used without prompt.

---

### Stage 2: Screen

```
STAGE 2: SCREEN
  [crypto]  Scanning exchange whitelist via CryptoPairSelector...
  [stocks]  Querying EODHD via StockScreenerModule (top 20)...
  [forex]   Checking OANDABackend instrument list + ADX/BB filter...

  ┌──────────────────────────────────────────────────────────┐
  │ Screening results                                        │
  │                                                          │
  │ CRYPTO (5 qualify)                                       │
  │   BTC/USDT  score=0.88  ADX=18.2  bb_w=0.031           │
  │   ETH/USDT  score=0.79  ADX=21.4  bb_w=0.028           │
  │   SOL/USDT  score=0.71  ADX=19.8  bb_w=0.025           │
  │   ...                                                    │
  │                                                          │
  │ STOCKS (8 qualify)                                       │
  │   AAPL      score=0.82  ADX=17.1  bb_w=0.019  RSI=52  │
  │   NVDA      score=0.79  ADX=22.3  bb_w=0.033  RSI=48  │
  │   ...                                                    │
  │                                                          │
  │ FOREX (3 qualify)                                        │
  │   EUR/USD   score=0.74  ADX=16.8  bb_w=0.008           │
  │   GBP/USD   score=0.68  ADX=19.2  bb_w=0.011           │
  │   ...                                                    │
  └──────────────────────────────────────────────────────────┘

  Select symbols to backtest:
  (comma-separated, 'all', 'top 3', or 'none' to skip a class)

  Crypto:  [BTC/USDT, ETH/USDT, SOL/USDT]: _
  Stocks:  [AAPL, NVDA]: _
  Forex:   [EUR/USD]: _
```

The bracketed defaults are the top results from the screen. Press Enter to accept.

---

### Stage 3: Backtest

```
STAGE 3: BACKTEST (30-day grid backtest)
  Running BTC/USDT...  done (2.4s)
  Running ETH/USDT...  done (2.1s)
  Running AAPL...      done (1.8s)
  Running NVDA...      done (1.9s)
  Running EUR/USD...   done (2.2s)

  ┌────────────────────────────────────────────────────────────────────┐
  │ Backtest results (30 days, grid_trading_v1)                        │
  │                                                                    │
  │ Symbol      Profit   Trades  Max DD   Sharpe  Recommendation      │
  │ ─────────── ──────── ─────── ──────── ─────── ────────────────── │
  │ BTC/USDT    +6.8%    18      -2.3%    1.42    TRADE               │
  │ ETH/USDT    +4.1%    14      -1.8%    1.21    TRADE               │
  │ AAPL        +4.2%    12      -1.1%    1.38    TRADE               │
  │ NVDA        -1.3%    8       -3.9%    0.61    SKIP (neg profit)   │
  │ EUR/USD     +2.1%    22      -0.9%    1.15    TRADE               │
  └────────────────────────────────────────────────────────────────────┘

  Symbols marked SKIP are excluded from defaults.
  Start trading these? (TRADE symbols as default)

  Final selection [BTC/USDT, ETH/USDT, AAPL, EUR/USD]: _
```

The recommendation column uses simple rules:
- `TRADE` if profit > 0% and max_drawdown < 5% and trades >= 5
- `SKIP (neg profit)` if profit <= 0%
- `SKIP (high dd)` if max_drawdown >= 5%
- `SKIP (low trades)` if trades < 5 (insufficient sample)

Skipped symbols are excluded from the default selection but the user can manually
add them back by typing the symbol name.

---

### Stage 4: Configure and Start

```
STAGE 4: CONFIGURE + START
  Writing grid configs...
  ✓ user_data/config.json updated
  ✓ Pair lists written

  Starting backends:
  ✓ FreqtradeBackend: BTC/USDT, ETH/USDT
    (freqtrade trade --config user_data/config.json --strategy OrchestratorStrategy)
  ✓ AlpacaBackend (paper): AAPL
    (alpaca backend running in separate process, PID 18423)
  ✓ OANDABackend (practice): EUR/USD
    (oanda backend running in separate process, PID 18424)

  All backends running. Morning setup complete.
  Log: user_data/logs/morning_run_2026-03-20.log
  State: user_data/morning_run_state.json
```

---

## State File Format

`user_data/morning_run_state.json` persists after each completed stage:

```json
{
  "date": "2026-03-20",
  "last_completed_stage": 3,
  "selected_markets": ["crypto", "stocks", "forex"],
  "screen_results": {
    "crypto": [{"symbol": "BTC/USDT", "score": 0.88}, ...],
    "stocks": [{"symbol": "AAPL", "score": 0.82}, ...],
    "forex":  [{"symbol": "EUR/USD", "score": 0.74}, ...]
  },
  "backtest_selections": ["BTC/USDT", "ETH/USDT", "AAPL", "EUR/USD"],
  "backtest_results": {
    "BTC/USDT": {"profit_pct": 6.8, "trades": 18, "max_dd": -2.3, "sharpe": 1.42},
    ...
  },
  "final_trading_symbols": ["BTC/USDT", "ETH/USDT", "AAPL", "EUR/USD"],
  "backends_started": {
    "freqtrade": {"pid": 18421, "symbols": ["BTC/USDT", "ETH/USDT"]},
    "alpaca": {"pid": 18423, "symbols": ["AAPL"]},
    "oanda": {"pid": 18424, "symbols": ["EUR/USD"]}
  }
}
```

If `last_completed_stage` is 3 and `--resume` is passed, the script skips to Stage 4
and asks for final confirmation before starting backends.

---

## Log Format

`user_data/logs/morning_run_YYYYMMDD.log` contains timestamped entries:

```
2026-03-20T07:30:01Z [INFO]  morning_run started (mode=interactive)
2026-03-20T07:30:02Z [INFO]  Stage 1: markets selected = ['crypto', 'stocks', 'forex']
2026-03-20T07:30:45Z [INFO]  Stage 2: screen complete — 16 candidates
2026-03-20T07:30:47Z [HUMAN] Stage 2 approval: user selected ['BTC/USDT','ETH/USDT','AAPL','NVDA','EUR/USD']
2026-03-20T07:31:02Z [INFO]  Stage 3: backtest complete
2026-03-20T07:31:03Z [HUMAN] Stage 3 approval: user confirmed ['BTC/USDT','ETH/USDT','AAPL','EUR/USD']
2026-03-20T07:31:05Z [INFO]  Stage 4: FreqtradeBackend started PID=18421
2026-03-20T07:31:06Z [INFO]  Stage 4: AlpacaBackend started PID=18423
2026-03-20T07:31:07Z [INFO]  Stage 4: OANDABackend started PID=18424
2026-03-20T07:31:07Z [INFO]  morning_run complete
```

`[HUMAN]` level entries record every user decision. `[INFO]` records mechanical steps.
This creates an audit trail of why specific symbols were traded each day.

---

## Dry Run Behaviour

With `--dry-run`:
- All screening API calls are replaced with fixture data (pre-loaded from
  `scripts/fixtures/dry_run_screen_results.json`)
- Backtest runs with `--timerange 20260101-20260120` against locally cached data
- No backends are started; the script prints the commands it would run
- State file is written to `user_data/morning_run_state_dryrun.json` (separate file,
  does not overwrite real state)

---

## File Locations

| File | Description |
|------|-------------|
| `scripts/morning_run.py` | Main CLI script |
| `scripts/fixtures/dry_run_screen_results.json` | Fixture data for `--dry-run` |
| `user_data/morning_run_state.json` | Runtime state (auto-created) |
| `user_data/logs/morning_run_YYYYMMDD.log` | Daily decision log (auto-created) |

---

## Acceptance Criteria

1. `scripts/morning_run.py` is executable (`chmod +x`) and runs with
   `python scripts/morning_run.py` from the project root.

2. Without flags, the script is fully interactive: it waits for user input at each
   stage before proceeding.

3. `--quick` flag: reads `morning_run_state.json` from the previous day, pre-fills
   all stage defaults with the previous session's selections, and asks only for
   confirmation (not fresh input) at each stage.

4. `--auto` flag: all stages run without prompts. All decisions default to the
   highest-scoring candidates. All choices are logged with `[AUTO]` level in the
   log file.

5. After each stage completes, `user_data/morning_run_state.json` is written with
   the current `last_completed_stage` value. If the script crashes mid-run, the
   state file reflects the last successfully completed stage.

6. `--resume` reads `morning_run_state.json`, identifies `last_completed_stage`,
   skips all prior stages, and resumes from the next stage with a summary of what
   was previously decided.

7. All human decisions (symbol selections, confirmations, edits) are written to
   `user_data/logs/morning_run_YYYYMMDD.log` with `[HUMAN]` prefix and UTC timestamp.

8. `--dry-run` completes the full four-stage sequence without making any real API
   calls. The state file written is `morning_run_state_dryrun.json`. No bots are
   started. The script prints the exact commands it would have run.

9. Symbols with negative backtest profit are excluded from the default final selection
   but can be manually added by the user (stage 3 prompt accepts manual input).

10. The recommendation logic (TRADE / SKIP rules) is in a standalone function with
    unit tests separate from the CLI interaction logic.

11. The script runs correctly when only a subset of backends are available. If
    `ALPACA_API_KEY` is not set, the script proceeds with crypto and forex only,
    prints a clear notice ("AlpacaBackend unavailable — stocks excluded"), and does
    not raise an error.

12. `--help` outputs a usage summary covering all flags.

---

## Out of Scope

- Web UI (CLI only in this story)
- Notification (Slack/Telegram) — that is a separate observability story
- Portfolio summary from previous day (future story: morning_run EOD report)
- Multiple config profiles support (single `user_data/config.json` assumed)

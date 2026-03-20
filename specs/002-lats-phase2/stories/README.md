# LATS Phase 2 — User Stories

Phase 1 built the core system (grid trading, orchestration, safety rails).
Phase 2 makes it production-ready: smarter, self-adapting, and observable.

## Stories

| ID | Title | Depends on | Priority |
|----|-------|-----------|---------|
| [US-P2-01](US-P2-01-candle-history-context.md) | Candle History in Module Context | — | P1 (foundation) |
| [US-P2-02](US-P2-02-config-simplification.md) | Streamlined Configuration (Profiles) | — | P1 |
| [US-P2-03](US-P2-03-adaptive-grid-sizing.md) | Adaptive Grid Sizing via ATR | US-P2-01 | P2 |
| [US-P2-04](US-P2-04-bayesian-live-update.md) | Bayesian Module Scoring with Live Feedback | — | P2 |
| [US-P2-05](US-P2-05-grid-state-observability.md) | Grid State Observability Dashboard | — | P2 |
| [US-P2-06](US-P2-06-grid-exit-strategy.md) | Grid Exit Strategy (Range Breakout) | US-P2-01 | P2 |

## Theme

Phase 1 asked: *"Can the system trade?"*
Phase 2 asks: *"Can the system learn, adapt, and tell you what it's doing?"*

- **US-P2-01 + US-P2-03**: Better data → better decisions. Modules get candle
  history; grids size themselves to current volatility.
- **US-P2-02**: Reduce the configuration surface area. One profile key replaces
  20+ fields for most operators.
- **US-P2-04**: Bayesian live updating. As real trades come in, the reasoning
  engine weights actual performance alongside indicator signals.
- **US-P2-05**: Visibility. Operators can see the grid's state without reading
  raw JSON or log files.
- **US-P2-06**: Graceful handling of regime change. When price permanently
  escapes the grid, the module exits or resets instead of doing nothing.

## Recommended Implementation Order

```
US-P2-01 (foundation) ──► US-P2-03 (ATR sizing)
                      └──► US-P2-06 (breakout exit)

US-P2-02 (config) — parallel, no deps

US-P2-04 (Bayesian) — parallel, no deps

US-P2-05 (observability) — parallel, no deps
```

---

## Phase 3 — Multi-Asset Expansion

Phase 2 asks: *"Can the system learn, adapt, and tell you what it's doing?"*
Phase 3 asks: *"Can the system trade anything — stocks, forex, crypto — through a single morning workflow?"*

| ID | Title | Depends on | Priority |
|----|-------|-----------|---------|
| [US-P3-01](US-P3-01-broker-backend-abstraction.md) | IBrokerBackend — Multi-Asset Execution Abstraction | — | P0 (foundation) |
| [US-P3-02](US-P3-02-integrated-stock-screener.md) | StockScreenerModule — EODHD Screening Inside LATS | US-P3-01 | P1 |
| [US-P3-03](US-P3-03-alpaca-backend.md) | AlpacaBackend — US Equity Execution | US-P3-01 | P1 |
| [US-P3-04](US-P3-04-forex-backend.md) | OANDABackend — Forex Execution | US-P3-01 | P2 |
| [US-P3-05](US-P3-05-morning-workflow-cli.md) | morning_run.py — Human-in-the-Loop Daily Workflow | US-P3-01 through US-P3-04 | P2 |

### Phase 3 Story Themes

- **US-P3-01 (IBrokerBackend)**: The mandatory foundation. Decouples LATS algorithm
  logic from freqtrade's crypto exchange abstraction. Every other Phase 3 story depends
  on this interface existing.

- **US-P3-02 (StockScreenerModule)**: Absorbs the standalone `eohd_screeener_max.py`
  script into the LATS module pipeline. Fixes the hardcoded API key security issue.
  Aligns equity screening criteria with `PairSelector` so `RuleBasedReasoningEngine`
  scores all asset classes consistently.

- **US-P3-03 (AlpacaBackend)**: Connects LATS signals to real (or paper) US equity
  order execution. Free Alpaca paper account; identical API for live. Enforces
  market-hours and double-confirmation guards against accidental live trading.

- **US-P3-04 (OANDABackend)**: Adds forex execution for 68 currency pairs via OANDA
  v20. Handles instrument name translation (EUR/USD ↔ EUR_USD) and 24/5 market hours
  differences from equities.

- **US-P3-05 (morning_run.py)**: The user-facing glue. One script drives the full
  Screen → Backtest → Approve → Trade sequence interactively. Supports `--quick`,
  `--auto`, `--dry-run`, and `--resume` for different operator workflows. Every human
  decision is logged for audit.

### Phase 3 Dependency Chain

```
US-P3-01 (IBrokerBackend — foundation)
    ├── US-P3-02 (StockScreenerModule)
    │       └── requires equity-capable backend to execute signals
    ├── US-P3-03 (AlpacaBackend — US equities)
    ├── US-P3-04 (OANDABackend — forex)
    └── US-P3-05 (morning_run.py — ties everything together)
            depends on all four above
```

### Phase 3 Recommended Implementation Order

```
US-P3-01 (foundation — implement first, blocks everything else)
    │
    ├── US-P3-02 (screener) ─┐
    ├── US-P3-03 (alpaca)   ─┤── can be built in parallel once P3-01 is done
    └── US-P3-04 (oanda)    ─┘
                               │
                        US-P3-05 (morning_run — implement last, integrates all)
```

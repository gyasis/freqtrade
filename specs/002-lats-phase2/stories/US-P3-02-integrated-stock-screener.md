# US-P3-02 — StockScreenerModule: EODHD Screening Inside LATS

**Phase:** 3
**Priority:** P1
**Depends on:** US-P3-01 (IBrokerBackend — for asset_class check and supports_live flag)

---

## Problem

`eohd_screeener_max.py` is a standalone script that runs in isolation. It has no knowledge
of LATS. Its output is a CSV file that a human then reads manually and decides which symbols
to trade. Separately, `PairSelector` applies ADX/BB/volume criteria to filter crypto pairs
for LATS modules. These two pieces are doing the same job — scoring and filtering tradeable
symbols — but in two disconnected systems for two different asset classes.

Specific issues with the current standalone script:

1. **Security**: EODHD API key is hardcoded in the script body (`6751a7dbc0dc70.26421855`).
   Any commit of the file exposes credentials.
2. **No integration**: output is a CSV; the orchestrator cannot consume it.
3. **Duplication**: `PairSelector` filtering logic (ADX < threshold, BB width range) is
   re-implemented separately in the screener using slightly different thresholds.
4. **No caching across LATS sessions**: the 24h cache lives on disk in `.cache/` relative
   to the script; it is not available to the SharedState store that modules share.

---

## Goal

Absorb the EODHD screener logic into a first-class LATS module:
`StockScreenerModule(IAlgoModule)`. It runs inside the orchestrator pipeline exactly like
`GridTradingModule` — registered, lifecycle-managed, and signal-emitting.

The module queries EODHD for top-N US equities, scores them using the same indicator
criteria that `RuleBasedReasoningEngine` applies to crypto, and returns `ModuleSignal`
entries for the top-K qualifying symbols. Those signals flow through `SignalArbiter` and
`BudgetAllocator` before becoming orders via an equity-capable backend (e.g.
`AlpacaBackend`).

The existing `eohd_screeener_max.py` root file is kept as a thin deprecation wrapper only.

---

## Module Identity

```python
module_id = "stock_screener_v1"
asset_class_requirement = "equity"  # refuses to run against FreqtradeBackend
supports_live = ModuleCapability.PARTIAL
    # PARTIAL because live equity execution requires AlpacaBackend (see US-P3-03)
    # Screener itself can run in dry-run mode against any backend to produce signals
```

---

## Screening Criteria (aligned with PairSelector)

| Criterion | Value | Source |
|-----------|-------|--------|
| ADX | < 25 | `PairSelectorCriteria.max_adx` equivalent |
| BB width | 0.005 – 0.20 | `PairSelectorCriteria` range |
| RSI | 40 – 60 | Neutral zone (range-bound confirmation) |
| Min 30-day avg daily volume | $1M USD | Liquidity floor |
| Exclude penny stocks | price > $5.00 | Hard filter |

These are the same thresholds used in `PairSelector` for crypto. Alignment is intentional:
the same `RuleBasedReasoningEngine` that routes crypto signals will route equity signals.

---

## Key Design Points

### API Key Security

```python
import os

EODHD_API_KEY = os.environ["EODHD_API_KEY"]
# Raises KeyError with a clear message if not set.
# Never read from config dict or hardcoded fallback.
```

If the environment variable is not set, the module raises `EnvironmentError` at
`on_bot_start` time with a message that explains how to set it:
```
StockScreenerModule: EODHD_API_KEY environment variable not set.
Set it with: export EODHD_API_KEY=<your_key>
```

### Rate Limiting (preserved from existing screener)

EODHD Technical API budget: 850 weight units per minute.
- RSI: 5 weight units per call
- ATR: 5 weight units per call
- MACD: 5 weight units per call

Use a token-bucket `RateLimiter` with `max_requests=850, window_seconds=60`. This is
identical to the limiter in the existing script, but the constructor parameter name is
corrected: **`max_requests`** (not `max_tokens` — the existing script has a `TypeError`
bug at line 634 where `RateLimiter(max_tokens=850, ...)` is called).

### Caching (preserved, integrated into SharedState)

24h price cache. Key: `"stock_screener_v1:price_cache:{date}"` in `SharedState`.
This replaces the `.cache/stock_prices_cache.csv` file used by the standalone script.
SharedState persistence means the cache survives bot restarts within the same trading day.

```python
cache_key = f"stock_screener_v1:price_cache:{today_iso}"
cached = ctx.shared_state.get(cache_key)
if cached:
    return cached
# ... fetch from EODHD ...
ctx.shared_state.set(cache_key, results, ttl_seconds=86400)
```

### Signal Format

```python
ModuleSignal(
    enter_long=True,
    entry_tag="stock_screener_v1:top_N_rank_K",
    # e.g. "stock_screener_v1:top_10_rank_3" means
    #      this symbol was rank 3 in a top-10 screening run
)
```

The `N` and `K` values in the tag are literal integers derived from `screener_top_n`
config and the symbol's rank in the scored list.

### Config Schema

```json
{
  "algo_system": {
    "modules": {
      "stock_screener_v1": {
        "screener_top_n": 10,
        "screener_emit_top_k": 3,
        "min_adx_max": 25,
        "rsi_low": 40,
        "rsi_high": 60,
        "min_price": 5.0,
        "min_avg_daily_volume_usd": 1000000
      }
    }
  }
}
```

All fields have defaults. `screener_top_n` (default 10) controls how many symbols EODHD
fetches. `screener_emit_top_k` (default 3) controls how many signals the module emits per
run (top K of the scored list).

---

## File Locations

| File | Description |
|------|-------------|
| `modules/stock_screener/stock_screener_module.py` | Main module (IAlgoModule subclass) |
| `modules/stock_screener/eodhd_client.py` | EODHD API thin client (rate-limited) |
| `modules/stock_screener/screener_criteria.py` | Scoring/filtering dataclass |
| `modules/stock_screener/__init__.py` | Package init |
| `eohd_screeener_max.py` (root, existing) | Deprecation wrapper only |

All module paths relative to `user_data/strategies/algo_system/`.

---

## Deprecation Wrapper

The existing `eohd_screeener_max.py` root file is updated to a thin shim:

```python
"""
eohd_screeener_max.py — DEPRECATED.

This standalone script has been superseded by StockScreenerModule.
Use it via the LATS orchestrator:

    # In your algo_system config:
    "active_modules": ["grid_trading_v1", "stock_screener_v1"]

    # Set your API key:
    export EODHD_API_KEY=<your_key>

This file will be removed in LATS v2.
"""
import warnings
warnings.warn(
    "eohd_screeener_max.py is deprecated. Use StockScreenerModule instead.",
    DeprecationWarning,
    stacklevel=2,
)
```

The original script body remains commented out below the warning (for reference during
the transition period), but is not executed.

---

## Acceptance Criteria

1. `StockScreenerModule` is located at
   `modules/stock_screener/stock_screener_module.py` and inherits from `IAlgoModule`.

2. `module_id` class attribute equals `"stock_screener_v1"`.

3. API key is loaded via `os.environ["EODHD_API_KEY"]`. If the variable is not set, the
   module raises `EnvironmentError` at `on_bot_start` with the message shown above.
   No hardcoded fallback key is present anywhere in the new code.

4. `screener_top_n` is read from module config with a default of 10. Configuring it to 5
   causes the module to fetch and score exactly 5 symbols from EODHD.

5. `generate_entry_signal` returns a list of `ModuleSignal` objects — one per qualifying
   symbol, up to `screener_emit_top_k`. Each signal has `enter_long=True` and
   `entry_tag` matching the format `"stock_screener_v1:top_{N}_rank_{K}"`.

6. Results are stored in `SharedState` under the key
   `"stock_screener_v1:price_cache:{YYYY-MM-DD}"` with a TTL of 86400 seconds. A second
   call to `generate_entry_signal` within the same day does not trigger a new EODHD API
   request.

7. `RateLimiter` is constructed with `max_requests=850` (not `max_tokens`). The TypeError
   bug from the existing script does not appear.

8. Unit tests use a mock EODHD HTTP response (fixture JSON). No real EODHD API calls
   are made during `pytest`. Tests cover:
   - Correct signal count when top-N symbols qualify
   - Cache hit path (no HTTP call on second invocation within same day)
   - `EnvironmentError` raised when `EODHD_API_KEY` is absent
   - Penny stock filter (price <= $5.00 excluded from results)
   - ADX filter (ADX >= 25 excluded from results)

9. `eohd_screeener_max.py` in the project root emits a `DeprecationWarning` on import
   and does not execute any screening logic.

10. `supports_live` is set to `ModuleCapability.PARTIAL`. Attempting to register this
    module with a `FreqtradeBackend` (asset_class="crypto") logs a warning:
    `"stock_screener_v1: asset_class mismatch — requires equity backend"` but does not
    crash the orchestrator (graceful degradation: module is registered but skipped during
    signal generation).

---

## Out of Scope

- Short-selling signals (only `enter_long` for now)
- Real-time intraday EODHD data (daily OHLCV only in this story)
- Alpaca order execution (see US-P3-03)
- Fundamental screening (P/E, EPS) — technical indicators only

---

## Migration Notes

Operators currently running `eohd_screeener_max.py` manually should:

1. Move their EODHD API key out of the script and into an environment variable:
   ```bash
   export EODHD_API_KEY=<your_key>
   ```
2. Add `"stock_screener_v1"` to `active_modules` in their algo config.
3. Add `AlpacaBackend` to their backend list (US-P3-03) to execute the signals.
4. Delete their `.cache/stock_prices_cache.csv` — SharedState handles caching now.

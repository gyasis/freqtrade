# US-P3-03 — AlpacaBackend: US Equity Execution

**Phase:** 3
**Priority:** P1
**Depends on:** US-P3-01 (IBrokerBackend ABC)

---

## Problem

`StockScreenerModule` (US-P3-02) can score and rank US equity symbols and emit entry
signals. But signals require a backend capable of executing equity orders. The existing
`FreqtradeBackend` wraps a crypto exchange — it has no concept of fractional shares, US
market hours, pattern-day-trader rules, or the equity settlement cycle (T+2).

There is no path from "LATS signals AAPL buy" to "AAPL order placed" today.

---

## Goal

Implement `AlpacaBackend(IBrokerBackend)` — a full execution backend for US equities
(and crypto) using the [Alpaca Trade API](https://alpaca.markets/docs/trading/).

Alpaca is chosen because:
- Free paper trading account with no minimum balance
- Identical API surface for paper and live — environment switching via one flag
- Supports both US equities and crypto in the same account
- REST + WebSocket APIs with generous free tier (200 requests/minute)
- Python SDK (`alpaca-trade-api` or the newer `alpaca-py`) is well maintained

---

## Alpaca API Coverage

| IBrokerBackend method | Alpaca endpoint |
|-----------------------|-----------------|
| `get_ohlcv` | `GET /v2/stocks/{symbol}/bars` or `/v2/crypto/bars` |
| `get_balance` | `GET /v2/account` → `cash`, `portfolio_value` |
| `place_order` | `POST /v2/orders` |
| `cancel_order` | `DELETE /v2/orders/{order_id}` |
| `get_open_positions` | `GET /v2/positions` |
| `get_symbols` | `GET /v2/assets?status=active&asset_class=us_equity` |

---

## Configuration

All credentials come from environment variables. No credentials in config files or code.

| Variable | Required | Description |
|----------|----------|-------------|
| `ALPACA_API_KEY` | Yes | Alpaca API key ID |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret key |
| `ALPACA_PAPER` | No | `"true"` (default) or `"false"` — controls paper vs live |

The default value of `ALPACA_PAPER` is `"true"`. Live trading requires explicitly setting
`ALPACA_PAPER=false`. This prevents accidental live order placement.

```python
import os

ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"
BASE_URL = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER
    else "https://api.alpaca.markets"
)
```

---

## OHLCV Column Mapping

Alpaca bar response fields map to freqtrade-standard OHLCV column names:

| Alpaca field | LATS column |
|--------------|-------------|
| `t` (timestamp) | `date` (UTC datetime) |
| `o` | `open` |
| `h` | `high` |
| `l` | `low` |
| `c` | `close` |
| `v` | `volume` |

Timeframe string mapping (LATS format → Alpaca format):

| LATS | Alpaca |
|------|--------|
| `"1m"` | `"1Min"` |
| `"5m"` | `"5Min"` |
| `"15m"` | `"15Min"` |
| `"1h"` | `"1Hour"` |
| `"4h"` | `"4Hour"` |
| `"1d"` | `"1Day"` |

---

## Rate Limiting

Alpaca free tier: 200 requests/minute.
Use a token-bucket `RateLimiter(max_requests=200, window_seconds=60)` (same class as
`StockScreenerModule` uses, from `algo_system/utils/rate_limiter.py`). Every HTTP call
to Alpaca passes through this limiter. If the budget is exhausted, the limiter sleeps
until the window refills rather than raising an error immediately, unless the wait would
exceed 30 seconds (in which case `RateLimitError` is raised).

---

## Market Hours Handling

Alpaca rejects market orders outside US equity market hours (09:30–16:00 ET, Monday–Friday).
`AlpacaBackend` enforces this at the `place_order` level:

- If `order_type == "market"` and the current time is outside market hours:
  raise `MarketClosedError(symbol, current_time)`.
- If `order_type == "limit"`: accepted at any time (good-till-cancelled).

`MarketClosedError` is a subclass of `BrokerBackendError` (defined in
`base/broker_exceptions.py`).

The orchestrator's `morning_run.py` (US-P3-05) uses this to gate equity signal processing
to pre-market screening + open-bell execution.

---

## Paper vs Live Safety Guard

A startup assertion prevents accidental live trading:

```python
def _assert_safe_to_trade(self) -> None:
    if not ALPACA_PAPER:
        confirmation = os.environ.get("ALPACA_LIVE_CONFIRMED", "")
        if confirmation != "I_UNDERSTAND_THIS_IS_LIVE_TRADING":
            raise RuntimeError(
                "AlpacaBackend: ALPACA_PAPER=false but "
                "ALPACA_LIVE_CONFIRMED is not set. "
                "Set ALPACA_LIVE_CONFIRMED=I_UNDERSTAND_THIS_IS_LIVE_TRADING "
                "to enable live trading."
            )
```

This double-confirmation prevents a misconfigured environment variable from
unintentionally enabling real-money execution.

---

## File Locations

| File | Description |
|------|-------------|
| `execution/alpaca_backend.py` | `AlpacaBackend` implementation |
| `base/broker_exceptions.py` | Add `MarketClosedError` here |
| `utils/rate_limiter.py` | Shared token-bucket (used by AlpacaBackend + StockScreenerModule) |

All paths relative to `user_data/strategies/algo_system/`.

---

## Acceptance Criteria

1. `AlpacaBackend` is located at `execution/alpaca_backend.py` and inherits from
   `IBrokerBackend`.

2. `asset_class` property returns `"equity"`.

3. Credentials are read exclusively from `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
   environment variables. If either is absent, `EnvironmentError` is raised at
   instantiation time with a message naming the missing variable.

4. `ALPACA_PAPER` defaults to `True`. Setting `ALPACA_PAPER=false` in the environment
   (without also setting `ALPACA_LIVE_CONFIRMED`) raises `RuntimeError` at instantiation.

5. `get_ohlcv("AAPL", "1h", 100)` returns a DataFrame with columns
   `["date", "open", "high", "low", "close", "volume"]`, 100 rows, sorted ascending.
   `date` column contains UTC-aware `datetime` objects.

6. `place_order("AAPL", "buy", 1.0, "market")` calls Alpaca `POST /v2/orders` with
   `{"symbol": "AAPL", "qty": 1.0, "side": "buy", "type": "market", ...}` and returns
   the normalized order dict.

7. `place_order` with `order_type="market"` outside market hours raises `MarketClosedError`.
   `place_order` with `order_type="limit"` outside market hours succeeds.

8. `cancel_order` calls `DELETE /v2/orders/{order_id}` and raises `OrderNotFoundError`
   if the API returns 404.

9. Rate limiter is initialized with `max_requests=200, window_seconds=60`.

10. Unit tests mock the Alpaca HTTP responses (no real network calls in pytest).
    Tests cover:
    - `get_ohlcv` column mapping correctness
    - `place_order` paper vs live URL selection
    - `MarketClosedError` raised for market orders outside hours
    - `ALPACA_LIVE_CONFIRMED` safety guard
    - `get_open_positions` returns normalized position dicts
    - Rate limiter invoked on every HTTP call

11. Tests run in CI without real Alpaca credentials (all HTTP mocked via `responses`
    or `httpx`).

---

## Out of Scope

- Short selling (no `can_short` support in this story)
- Options or ETF-specific handling
- WebSocket streaming quotes (REST polling only for now)
- Portfolio rebalancing logic (handled by BudgetAllocator at orchestrator level)
- Crypto execution via Alpaca (freqtrade handles crypto; Alpaca crypto is a future story)

---

## Notes on alpaca-py vs alpaca-trade-api

The legacy `alpaca-trade-api` package is in maintenance mode as of 2024. Use the newer
`alpaca-py` package (`pip install alpaca-py`). The `AlpacaBackend` implementation should
use `alpaca-py`'s `TradingClient` and `StockHistoricalDataClient` directly rather than
building raw HTTP requests, to benefit from automatic retry and auth handling.

Add `alpaca-py` to `requirements.txt` under a `# LATS Phase 3 extras` comment block
(not mixed with core freqtrade dependencies).

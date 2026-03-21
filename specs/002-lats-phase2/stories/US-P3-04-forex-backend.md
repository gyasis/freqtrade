# US-P3-04 — OANDABackend: Forex Execution

**Phase:** 3
**Priority:** P2
**Depends on:** US-P3-01 (IBrokerBackend ABC)

---

## Problem

LATS can compute grid levels for EUR/USD and run `RuleBasedReasoningEngine` scoring on
forex candle data. It cannot place or manage a forex order. The forex market is a
distinct asset class with its own settlement, margin, and instrument naming conventions
that freqtrade's crypto-oriented exchange abstraction does not support.

Without an execution backend for forex, the multi-asset vision of Phase 3 is incomplete:
users running `morning_run.py` (US-P3-05) can trade crypto and US equities but have no
path to the $7.5 trillion/day forex market.

---

## Goal

Implement `OANDABackend(IBrokerBackend)` — a full execution backend for forex using the
[OANDA v20 REST API](https://developer.oanda.com/rest-live-v20/introduction/).

OANDA is chosen because:
- Free practice account, identical API surface to live
- 68 major, minor, and exotic currency pairs
- Supports fractional units (micro-lots)
- Well-documented v20 REST API with official Python library (`v20`)
- Widely used in retail algo trading

---

## OANDA API Coverage

| IBrokerBackend method | OANDA v20 endpoint |
|-----------------------|--------------------|
| `get_ohlcv` | `GET /v3/instruments/{instrument}/candles` |
| `get_balance` | `GET /v3/accounts/{accountID}/summary` → `balance`, `NAV` |
| `place_order` | `POST /v3/accounts/{accountID}/orders` |
| `cancel_order` | `PUT /v3/accounts/{accountID}/orders/{orderSpecifier}/cancel` |
| `get_open_positions` | `GET /v3/accounts/{accountID}/openPositions` |
| `get_symbols` | `GET /v3/accounts/{accountID}/instruments` |

---

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `OANDA_API_KEY` | Yes | OANDA v20 API access token |
| `OANDA_ACCOUNT_ID` | Yes | OANDA account ID (format: `"001-001-XXXXXXX-001"`) |
| `OANDA_PRACTICE` | No | `"true"` (default) or `"false"` — practice vs live |

Practice API host: `https://api-fxpractice.oanda.com`
Live API host:     `https://api-fxtrade.oanda.com`

Same double-confirmation guard as AlpacaBackend:

```python
if not OANDA_PRACTICE:
    if os.environ.get("OANDA_LIVE_CONFIRMED") != "I_UNDERSTAND_THIS_IS_LIVE_TRADING":
        raise RuntimeError(
            "OANDABackend: OANDA_PRACTICE=false but OANDA_LIVE_CONFIRMED not set."
        )
```

---

## Instrument Name Mapping

OANDA uses underscore-separated instrument names. LATS uses slash-separated pairs.
`OANDABackend` translates transparently in both directions:

| LATS format | OANDA format |
|-------------|--------------|
| `"EUR/USD"` | `"EUR_USD"` |
| `"GBP/JPY"` | `"GBP_JPY"` |
| `"USD/CAD"` | `"USD_CAD"` |

Conversion logic:

```python
def _to_oanda(self, symbol: str) -> str:
    return symbol.replace("/", "_")

def _from_oanda(self, instrument: str) -> str:
    return instrument.replace("_", "/")
```

`get_symbols()` calls `_from_oanda` on all returned instruments so the orchestrator
always sees LATS-format names.

---

## OHLCV Column Mapping

| OANDA candle field | LATS column |
|--------------------|-------------|
| `time` | `date` (UTC datetime, parsed from RFC3339 string) |
| `mid.o` | `open` |
| `mid.h` | `high` |
| `mid.l` | `low` |
| `mid.c` | `close` |
| `volume` | `volume` |

OANDA provides bid/ask/mid prices. `OANDABackend` uses `mid` prices by default for
OHLCV construction. This is appropriate for retail algorithmic strategy backtesting
and grid signal generation. Spread-aware execution is out of scope for this story.

---

## Timeframe Mapping

| LATS | OANDA `granularity` |
|------|---------------------|
| `"1m"` | `"M1"` |
| `"5m"` | `"M5"` |
| `"15m"` | `"M15"` |
| `"30m"` | `"M30"` |
| `"1h"` | `"H1"` |
| `"4h"` | `"H4"` |
| `"1d"` | `"D"` |
| `"1w"` | `"W"` |

---

## Order Mapping

OANDA orders are placed with a `units` field (positive = buy, negative = sell):

```python
def place_order(self, symbol, side, amount, order_type, price=None):
    units = amount if side == "buy" else -amount
    order_body = {
        "order": {
            "type": "MARKET" if order_type == "market" else "LIMIT",
            "instrument": self._to_oanda(symbol),
            "units": str(units),
        }
    }
    if order_type == "limit" and price is not None:
        order_body["order"]["price"] = str(price)
    # POST to OANDA orders endpoint...
```

The returned order dict is normalized to the standard `IBrokerBackend` format before
being returned to the orchestrator.

---

## Forex Market Hours

The forex market is open 24 hours/day, 5 days/week (Sunday 17:00 ET – Friday 17:00 ET).
`OANDABackend` does not enforce market-hour restrictions for forex — this is intentional
and differs from `AlpacaBackend`. However, during weekend close:

- Market orders are accepted by OANDA but queued for execution at Sunday open.
- `OANDABackend` logs a warning when orders are placed during weekend hours:
  `"OANDABackend: market is closed — order will queue for Sunday open."`

---

## Position Data Normalization

`get_open_positions()` normalizes OANDA's position structure to the standard format:

```python
# OANDA response structure (simplified)
{
    "instrument": "EUR_USD",
    "long": {"units": "10000", "averagePrice": "1.08432", "unrealizedPL": "45.20"},
    "short": {"units": "0", ...}
}

# Normalized output
{
    "symbol": "EUR/USD",
    "side": "buy",
    "amount": 10000.0,
    "entry_price": 1.08432,
    "unrealized_pnl": 45.20
}
```

Only positions with non-zero units are returned.

---

## File Locations

| File | Description |
|------|-------------|
| `execution/oanda_backend.py` | `OANDABackend` implementation |
| `base/broker_exceptions.py` | No new exceptions needed (reuse existing hierarchy) |

All paths relative to `user_data/strategies/algo_system/`.

Add `v20` (OANDA's official Python library) to `requirements.txt` under
`# LATS Phase 3 extras`.

---

## Acceptance Criteria

1. `OANDABackend` is located at `execution/oanda_backend.py` and inherits from
   `IBrokerBackend`.

2. `asset_class` property returns `"forex"`.

3. Credentials are read from `OANDA_API_KEY` and `OANDA_ACCOUNT_ID` environment
   variables. Missing either raises `EnvironmentError` at instantiation.

4. `OANDA_PRACTICE` defaults to `True`. Live mode requires `OANDA_LIVE_CONFIRMED`
   double-confirmation (same guard as AlpacaBackend).

5. `_to_oanda("EUR/USD")` returns `"EUR_USD"`. `_from_oanda("GBP_JPY")` returns
   `"GBP/JPY"`.

6. `get_ohlcv("EUR/USD", "1h", 200)` calls OANDA's candles endpoint with
   `instrument=EUR_USD`, `granularity=H1`, and `count=200`. Returns a DataFrame with
   correct LATS column names, 200 rows, sorted ascending by `date`.

7. `place_order("EUR/USD", "buy", 10000.0, "market")` sends a POST with
   `units="10000"` (positive). `place_order("EUR/USD", "sell", 5000.0, "market")`
   sends `units="-5000"` (negative).

8. `get_open_positions()` returns normalized dicts with LATS-format symbol names
   (e.g. `"EUR/USD"`, not `"EUR_USD"`).

9. Weekend market-closed warning is logged when `place_order` is called on Saturday
   or Sunday UTC. No error is raised.

10. Unit tests mock all OANDA HTTP responses. No real OANDA API calls in pytest. Tests
    cover:
    - Instrument name conversion in both directions
    - `get_ohlcv` column mapping and row count
    - `place_order` units sign for buy vs sell
    - `cancel_order` with `OrderNotFoundError` on 404
    - `get_open_positions` normalization (OANDA format → standard format)
    - Practice vs live URL selection
    - `OANDA_LIVE_CONFIRMED` guard

11. Tests run in CI without real OANDA credentials.

---

## Out of Scope

- Bid/ask spread modelling
- Streaming price quotes (WebSocket)
- Margin and leverage configuration (OANDA defaults apply)
- Guaranteed stop-loss orders (OANDA premium feature)
- Multiple account IDs (single `OANDA_ACCOUNT_ID` only)

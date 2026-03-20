# US-P3-01 — IBrokerBackend: Multi-Asset Execution Abstraction

**Phase:** 3
**Priority:** P0 (foundation — all other P3 stories depend on this)
**Depends on:** None (but enables US-P3-02, US-P3-03, US-P3-04)

---

## Problem

`OrchestratorStrategy` inherits from freqtrade's `IStrategy`, which means the entire LATS
algorithm stack — `GridTradingModule`, `RuleBasedReasoningEngine`, `BudgetAllocator`,
`SignalArbiter` — is coupled to freqtrade's internal exchange abstraction. That abstraction
was designed for crypto exchanges; it has no concept of equity tickers, forex instruments,
or fractional shares.

The algorithm logic itself is entirely asset-agnostic. `GridTradingModule` does not care
whether the symbol is `BTC/USDT` or `AAPL`. `RuleBasedReasoningEngine` scores signals the
same way regardless of asset class. But because the execution path goes through
`IStrategy.wallets` and freqtrade's `DataProvider`, there is no way to run these modules
against US stocks or forex without forking the entire codebase and duplicating significant
logic.

Today's architecture:

```
IStrategy (freqtrade) <── OrchestratorStrategy <── [all LATS modules]
```

Goal architecture:

```
IBrokerBackend (ABC) <── FreqtradeBackend (wraps IStrategy)
                     <── AlpacaBackend    (US equities)
                     <── OANDABackend     (forex)
                     <── MockBrokerBackend (tests)

OrchestratorStrategy uses IBrokerBackend, not IStrategy directly
```

---

## Goal

Define `IBrokerBackend`, an Abstract Base Class that provides a uniform interface for:
- OHLCV data retrieval
- Balance and position queries
- Order placement and cancellation
- Symbol enumeration

`OrchestratorStrategy` becomes a `FreqtradeBackend` wrapper that satisfies this contract
while delegating to freqtrade's existing internals. New execution backends (Alpaca, OANDA)
are drop-in replacements: the LATS orchestrator sees only `IBrokerBackend` and never
knows which broker is behind it.

---

## Interface Specification

```python
# user_data/strategies/algo_system/base/broker_backend.py

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Literal

from pandas import DataFrame


AssetClass = Literal["crypto", "equity", "forex"]


class IBrokerBackend(ABC):
    """
    Uniform execution interface for all asset classes.

    Implementors: FreqtradeBackend, AlpacaBackend, OANDABackend, MockBrokerBackend.
    """

    @property
    @abstractmethod
    def asset_class(self) -> AssetClass:
        """
        Returns the primary asset class this backend handles.
        "crypto" | "equity" | "forex"
        """

    @abstractmethod
    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        n_candles: int,
    ) -> DataFrame:
        """
        Return a DataFrame with columns:
          date (datetime, UTC), open, high, low, close, volume
        Rows are sorted ascending by date. Must return at least n_candles rows
        or raise InsufficientDataError.
        """

    @abstractmethod
    def get_balance(self) -> Dict[str, float]:
        """
        Return available balances as {currency: amount}.
        For equities: {"USD": 12345.67}
        For crypto:   {"USDT": 5000.0, "BTC": 0.1}
        """

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,          # "buy" | "sell"
        amount: float,      # quantity in base currency / shares
        order_type: str,    # "market" | "limit"
        price: float | None = None,
    ) -> dict:
        """
        Place an order. Returns a dict with at minimum:
          {"order_id": str, "status": str, "symbol": str,
           "side": str, "amount": float, "filled": float, "price": float}
        """

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel an open order. Raise OrderNotFoundError if order does not exist."""

    @abstractmethod
    def get_open_positions(self) -> List[dict]:
        """
        Return a list of open positions.
        Each entry: {"symbol": str, "side": str, "amount": float,
                     "entry_price": float, "unrealized_pnl": float}
        """

    @abstractmethod
    def get_symbols(self) -> List[str]:
        """
        Return all tradeable symbols for this backend.
        Format: exchange-native (e.g. "BTC/USDT", "AAPL", "EUR_USD").
        """
```

### Exception hierarchy

```python
# user_data/strategies/algo_system/base/broker_exceptions.py

class BrokerBackendError(Exception):
    """Base for all broker backend errors."""

class InsufficientDataError(BrokerBackendError):
    """get_ohlcv returned fewer candles than requested."""

class OrderNotFoundError(BrokerBackendError):
    """cancel_order called for an order that does not exist."""

class InsufficientFundsError(BrokerBackendError):
    """place_order rejected due to insufficient balance."""

class RateLimitError(BrokerBackendError):
    """Backend rate limit exceeded. Callers should back off."""
```

---

## FreqtradeBackend (wrapper for existing code)

`FreqtradeBackend` wraps `OrchestratorStrategy`'s existing exchange access. Its
`__init__` receives the strategy instance and delegates to freqtrade's internal
DataProvider and exchange client.

Key constraint: **existing OrchestratorStrategy tests must continue to pass without
modification.** `FreqtradeBackend` is an adapter layer, not a rewrite.

```python
# user_data/strategies/algo_system/execution/freqtrade_backend.py

class FreqtradeBackend(IBrokerBackend):
    asset_class = "crypto"

    def __init__(self, strategy: "OrchestratorStrategy") -> None:
        self._strategy = strategy

    def get_ohlcv(self, symbol, timeframe, n_candles):
        # delegates to self._strategy.dp.get_pair_dataframe(...)
        ...

    def get_balance(self):
        # delegates to self._strategy.wallets.get_free(...)
        ...
    # ... etc
```

---

## MockBrokerBackend (for unit tests)

```python
# user_data/strategies/algo_system/execution/mock_backend.py

class MockBrokerBackend(IBrokerBackend):
    """
    In-memory broker backend for unit tests.
    No exchange connection required.
    Configurable with preset OHLCV fixtures and balance.
    """

    def __init__(
        self,
        ohlcv_fixture: DataFrame | None = None,
        initial_balance: Dict[str, float] | None = None,
        asset_class: AssetClass = "crypto",
    ) -> None: ...

    # Records all placed/cancelled orders for assertion in tests
    @property
    def order_history(self) -> List[dict]: ...
```

---

## File Locations

| File | Description |
|------|-------------|
| `base/broker_backend.py` | `IBrokerBackend` ABC + `AssetClass` type alias |
| `base/broker_exceptions.py` | Exception hierarchy |
| `execution/freqtrade_backend.py` | Freqtrade adapter (wraps OrchestratorStrategy) |
| `execution/mock_backend.py` | Test double |

All paths relative to `user_data/strategies/algo_system/`.

---

## Acceptance Criteria

1. `IBrokerBackend` ABC lives at `base/broker_backend.py` with all 6 abstract methods
   as specified above.

2. Attempting to instantiate `IBrokerBackend` directly raises `TypeError` (standard ABC
   enforcement — no extra code needed).

3. `FreqtradeBackend` wraps the existing `OrchestratorStrategy` internal exchange access.
   All existing Phase 1 and Phase 2 tests pass without modification after this change.

4. `MockBrokerBackend` satisfies all ABC contracts. Its `order_history` list records every
   `place_order` and `cancel_order` call for test assertion.

5. `IBrokerBackend.asset_class` property returns a value from the `AssetClass` literal type.
   `FreqtradeBackend.asset_class` returns `"crypto"`.

6. `get_ohlcv` contract: returned DataFrame always has columns
   `["date", "open", "high", "low", "close", "volume"]` sorted ascending by `date`.
   `InsufficientDataError` is raised if the backend cannot fulfil `n_candles`.

7. Unit test: `MockBrokerBackend` receives an OHLCV fixture with 50 rows; calling
   `get_ohlcv("BTC/USDT", "1h", 50)` returns a DataFrame with correct column names and
   50 rows.

8. Unit test: `MockBrokerBackend.place_order(...)` records the order in `order_history`
   and returns a dict with all required keys.

9. Unit test: instantiating a class that inherits `IBrokerBackend` but omits one abstract
   method raises `TypeError` — confirming the ABC contract is enforced.

10. No changes to `OrchestratorStrategy`'s public API or its freqtrade hook signatures
    (`populate_indicators`, `populate_entry_trend`, etc.).

---

## Out of Scope

- Real Alpaca or OANDA integration (see US-P3-03, US-P3-04)
- Multi-backend routing (running FreqtradeBackend and AlpacaBackend simultaneously)
- Order fill simulation in MockBrokerBackend (stub returns immediately)

---

## Dependency Chain

```
US-P3-01 (this story — IBrokerBackend)
    ├── US-P3-02 (StockScreenerModule uses IBrokerBackend.asset_class check)
    ├── US-P3-03 (AlpacaBackend implements IBrokerBackend)
    ├── US-P3-04 (OANDABackend implements IBrokerBackend)
    └── US-P3-05 (morning_run.py instantiates backends by type)
```

"""
tests/harness/mock_exchange.py
Minimal exchange stub for LATS test isolation.
Provides order-book simulation, balance tracking, and order fill simulation.
Not a full freqtrade IExchange implementation — only the surface needed by
backtest harness and unit tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("algo_system.test.mock_exchange")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class MockBalance:
    free: float = 0.0
    used: float = 0.0

    @property
    def total(self) -> float:
        return self.free + self.used


@dataclass
class MockOrder:
    order_id: str
    pair: str
    side: str          # "buy" | "sell"
    amount: float      # base currency
    price: float       # limit price (0 = market)
    status: str = "open"   # open | closed | canceled
    filled: float = 0.0
    cost: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.order_id,
            "pair": self.pair,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "status": self.status,
            "filled": self.filled,
            "cost": self.cost,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# MockExchange
# ---------------------------------------------------------------------------

class MockExchange:
    """
    Deterministic exchange stub for unit and harness testing.

    Supports:
    - Balance tracking (stake + base currency)
    - Limit-order placement and fill simulation
    - Order book snapshot (constant spread)
    - Fill-all-open-orders helper for test step simulation
    """

    def __init__(
        self,
        stake_currency: str = "USDT",
        initial_balance: float = 10_000.0,
        spread_pct: float = 0.001,
    ) -> None:
        self._stake_currency = stake_currency
        self._spread_pct = spread_pct
        self._balances: Dict[str, MockBalance] = {
            stake_currency: MockBalance(free=initial_balance),
        }
        self._open_orders: Dict[str, MockOrder] = {}   # order_id -> order
        self._filled_orders: List[MockOrder] = []
        self._order_counter: int = 0
        self._current_price: Dict[str, float] = {}    # pair -> price

    # ------------------------------------------------------------------
    # Price feed
    # ------------------------------------------------------------------

    def set_price(self, pair: str, price: float) -> None:
        """Update the mock market price for *pair*."""
        self._current_price[pair] = price

    def get_price(self, pair: str) -> float:
        return self._current_price.get(pair, 0.0)

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------

    def fetch_order_book(self, pair: str, limit: int = 5) -> dict:
        price = self.get_price(pair)
        spread = price * self._spread_pct / 2
        asks = [[price + spread * (i + 1), 1.0] for i in range(limit)]
        bids = [[price - spread * (i + 1), 1.0] for i in range(limit)]
        return {"asks": asks, "bids": bids, "symbol": pair}

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def fetch_balance(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"total": {}, "free": {}, "used": {}}
        for currency, bal in self._balances.items():
            result["total"][currency] = bal.total
            result["free"][currency] = bal.free
            result["used"][currency] = bal.used
        return result

    def get_free_balance(self, currency: str) -> float:
        return self._balances.get(currency, MockBalance()).free

    def get_total_balance(self, currency: str) -> float:
        return self._balances.get(currency, MockBalance()).total

    def _ensure_currency(self, currency: str) -> None:
        if currency not in self._balances:
            self._balances[currency] = MockBalance()

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def _next_order_id(self) -> str:
        self._order_counter += 1
        return f"mock-order-{self._order_counter:06d}"

    def create_order(
        self,
        pair: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        order_type: str = "limit",
    ) -> MockOrder:
        """Place an order. Market orders fill immediately at current price."""
        oid = self._next_order_id()
        fill_price = price if price else self.get_price(pair)

        order = MockOrder(
            order_id=oid,
            pair=pair,
            side=side,
            amount=amount,
            price=fill_price,
        )

        if order_type == "market":
            self._fill_order(order, fill_price)
        else:
            self._open_orders[oid] = order
            # Lock balance
            base_currency = pair.split("/")[0]
            self._ensure_currency(base_currency)
            if side == "buy":
                cost = amount * fill_price
                self._balances[self._stake_currency].free -= cost
                self._balances[self._stake_currency].used += cost
            else:
                self._balances[base_currency].free -= amount
                self._balances[base_currency].used += amount

        logger.debug(
            "Order %s: %s %s %.6f @ %.4f (type=%s)",
            oid, side, pair, amount, fill_price, order_type,
        )
        return order

    def _fill_order(self, order: MockOrder, fill_price: float) -> None:
        order.price = fill_price
        order.filled = order.amount
        order.cost = order.amount * fill_price
        order.status = "closed"

        base_currency = order.pair.split("/")[0]
        self._ensure_currency(base_currency)

        if order.side == "buy":
            # Release locked stake, add base currency
            self._balances[self._stake_currency].used -= order.cost
            self._balances[base_currency].free += order.amount
        else:
            # Release locked base, add stake
            self._balances[base_currency].used -= order.amount
            self._balances[self._stake_currency].free += order.cost

        self._filled_orders.append(order)
        if order.order_id in self._open_orders:
            del self._open_orders[order.order_id]

    def fill_open_orders(self, pair: Optional[str] = None) -> List[MockOrder]:
        """
        Simulate fill of all open limit orders at their limit price.
        Optionally filter to a specific *pair*.
        Returns list of filled orders.
        """
        to_fill = [
            o for o in list(self._open_orders.values())
            if pair is None or o.pair == pair
        ]
        for order in to_fill:
            self._fill_order(order, order.price)
        return to_fill

    def cancel_order(self, order_id: str) -> Optional[MockOrder]:
        order = self._open_orders.pop(order_id, None)
        if order is None:
            return None
        order.status = "canceled"
        base_currency = order.pair.split("/")[0]
        self._ensure_currency(base_currency)
        # Release locked balances
        if order.side == "buy":
            cost = order.amount * order.price
            self._balances[self._stake_currency].used -= cost
            self._balances[self._stake_currency].free += cost
        else:
            self._balances[base_currency].used -= order.amount
            self._balances[base_currency].free += order.amount
        return order

    def fetch_order(self, order_id: str) -> Optional[dict]:
        order = self._open_orders.get(order_id)
        if order:
            return order.to_dict()
        # Check filled orders
        for o in self._filled_orders:
            if o.order_id == order_id:
                return o.to_dict()
        return None

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def open_orders(self) -> List[MockOrder]:
        return list(self._open_orders.values())

    @property
    def filled_orders(self) -> List[MockOrder]:
        return list(self._filled_orders)

    def reset(self) -> None:
        """Reset exchange to initial state (for test isolation between cases)."""
        for bal in self._balances.values():
            bal.free = 0.0
            bal.used = 0.0
        self._open_orders.clear()
        self._filled_orders.clear()
        self._order_counter = 0
        self._current_price.clear()

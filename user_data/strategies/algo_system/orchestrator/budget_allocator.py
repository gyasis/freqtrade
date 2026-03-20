"""
budget_allocator.py — T009 LATS System

Per-module capital and trade-slot accounting for the algo_system orchestrator.

Responsibilities:
  - Register each trading module with its max open-trade count and
    fractional stake allocation.
  - Gate every trade-open request so that neither the slot quota nor the
    capital cap for a module can be breached.
  - Track running counters so the orchestrator can query real-time
    utilization without re-scanning open trades.
  - Support suspension / resumption of modules (e.g., triggered by the
    safety layer) while preserving their reserved quota.

Thread Safety:
  All mutations and reads that span multiple fields are performed inside
  a single ``threading.Lock``, making BudgetAllocator safe for use from
  freqtrade's callback threads.
"""

import copy
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

logger = logging.getLogger("algo_system.budget")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModuleBudget:
    """Live budget state for one trading module.

    Attributes
    ----------
    module_id:
        Unique identifier matching the module's own ``module_id`` attribute.
    max_open_trades:
        Maximum number of concurrently open trades this module may hold.
    stake_allocation_pct:
        Fraction of total capital allocated to this module (0.0 – 1.0).
    current_open_trades:
        Running count of currently open trades.  Updated by
        ``BudgetAllocator.record_open`` / ``record_close``.
    current_stake_used:
        Sum of stake amounts currently deployed by this module.
    is_suspended:
        When ``True`` the module's quota is reserved but no new trades are
        permitted.  Set via ``record_suspension`` / ``record_resumption``.
    """

    module_id: str
    max_open_trades: int
    stake_allocation_pct: float  # fraction 0.0–1.0
    current_open_trades: int = field(default=0)
    current_stake_used: float = field(default=0.0)
    is_suspended: bool = field(default=False)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if not self.module_id:
            raise ValueError("module_id must be a non-empty string")
        if self.max_open_trades < 1:
            raise ValueError(
                f"max_open_trades must be >= 1, got {self.max_open_trades}"
            )
        if not (0.0 < self.stake_allocation_pct <= 1.0):
            raise ValueError(
                f"stake_allocation_pct must be in (0.0, 1.0], "
                f"got {self.stake_allocation_pct}"
            )

    # ------------------------------------------------------------------
    # Core gate
    # ------------------------------------------------------------------

    def can_open_trade(self, proposed_stake: float, total_capital: float) -> bool:
        """Return ``True`` only if all budget constraints are satisfied.

        This is a *pure* predicate — it does **not** mutate any counter.
        The BudgetAllocator is responsible for calling ``record_open``
        after a positive gate decision is acted upon.

        Parameters
        ----------
        proposed_stake:
            The amount (in quote currency) that the new trade would commit.
        total_capital:
            The current total bot capital used to resolve the absolute cap
            from ``stake_allocation_pct``.
        """
        if self.is_suspended:
            return False
        if self.current_open_trades >= self.max_open_trades:
            return False
        allocated_max = total_capital * self.stake_allocation_pct
        if self.current_stake_used + proposed_stake > allocated_max:
            return False
        return True


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------


class BudgetAllocator:
    """Central budget registry and gating layer for the algo_system.

    Usage pattern
    -------------
    Instantiated once inside the master strategy.  Each sub-module registers
    itself at strategy startup::

        allocator.register_module("grid_01", max_open_trades=4,
                                  stake_allocation_pct=0.25)

    Before the strategy calls ``custom_stake_amount`` or approves a signal it
    checks permission::

        allowed, reason = allocator.can_open_trade(
            "grid_01", proposed_stake=50.0, total_capital=1000.0
        )
        if not allowed:
            return []  # suppress signal

    After the exchange confirms the order opened::

        allocator.record_open("grid_01", stake=50.0)

    After the trade closes (``confirm_trade_exit`` or equivalent)::

        allocator.record_close("grid_01", stake=50.0)
    """

    def __init__(self) -> None:
        self._budgets: Dict[str, ModuleBudget] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_module(
        self,
        module_id: str,
        max_open_trades: int,
        stake_allocation_pct: float,
    ) -> None:
        """Register a module's budget parameters.

        Parameters
        ----------
        module_id:
            Unique string identifier for the module.
        max_open_trades:
            Maximum simultaneously open trades the module may hold.
        stake_allocation_pct:
            Fraction of total bot capital this module may deploy (0.0 < x <= 1.0).

        Raises
        ------
        ValueError
            If a module with the same ``module_id`` has already been
            registered, or if parameter values are out of range.
        """
        with self._lock:
            if module_id in self._budgets:
                raise ValueError(
                    f"Module '{module_id}' is already registered.  "
                    "Call record_suspension/resumption to manage its state, "
                    "not re-registration."
                )
            budget = ModuleBudget(
                module_id=module_id,
                max_open_trades=max_open_trades,
                stake_allocation_pct=stake_allocation_pct,
            )
            self._budgets[module_id] = budget
            logger.info(
                "Registered module '%s': max_open_trades=%d, "
                "stake_allocation_pct=%.4f",
                module_id,
                max_open_trades,
                stake_allocation_pct,
            )

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    def can_open_trade(
        self,
        module_id: str,
        proposed_stake: float,
        total_capital: float,
    ) -> Tuple[bool, str]:
        """Gate a trade-open request for the given module.

        Parameters
        ----------
        module_id:
            The module requesting to open a trade.
        proposed_stake:
            Quote-currency amount the new trade would commit.
        total_capital:
            Current total bot capital (used to resolve the absolute
            allocation cap).

        Returns
        -------
        (allowed, reason) :
            ``allowed`` is ``True`` when the trade may proceed.
            ``reason`` is an empty string on success, or a short rejection
            code / message on failure.  Rejections are also logged at
            WARNING level so they surface in freqtrade's log stream.

        Rejection reason strings
        ------------------------
        ``"module_not_registered"``
            ``module_id`` was never passed to ``register_module``.
        ``"module_suspended"``
            The module is currently suspended.
        ``"trade_quota_exceeded (N/M)"``
            ``N`` trades are already open against a maximum of ``M``.
        ``"stake_cap_exceeded (used/max USDT)"``
            Adding the proposed stake would breach the module's capital cap.
        """
        with self._lock:
            if proposed_stake <= 0:
                reason = f"invalid_stake ({proposed_stake:.4f} must be > 0)"
                logger.warning(
                    "can_open_trade rejected for '%s': %s",
                    module_id,
                    reason,
                )
                return False, reason

            budget = self._budgets.get(module_id)

            if budget is None:
                reason = "module_not_registered"
                logger.warning(
                    "can_open_trade rejected for '%s': %s",
                    module_id,
                    reason,
                )
                return False, reason

            if budget.is_suspended:
                reason = "module_suspended"
                logger.warning(
                    "can_open_trade rejected for '%s': %s",
                    module_id,
                    reason,
                )
                return False, reason

            if budget.current_open_trades >= budget.max_open_trades:
                reason = (
                    f"trade_quota_exceeded "
                    f"({budget.current_open_trades}/{budget.max_open_trades})"
                )
                logger.warning(
                    "can_open_trade rejected for '%s': %s",
                    module_id,
                    reason,
                )
                return False, reason

            allocated_max = total_capital * budget.stake_allocation_pct
            if budget.current_stake_used + proposed_stake > allocated_max:
                reason = (
                    f"stake_cap_exceeded "
                    f"({budget.current_stake_used + proposed_stake:.2f}"
                    f"/{allocated_max:.2f} USDT)"
                )
                logger.warning(
                    "can_open_trade rejected for '%s': %s",
                    module_id,
                    reason,
                )
                return False, reason

            return True, ""

    # ------------------------------------------------------------------
    # Counter mutations
    # ------------------------------------------------------------------

    def record_open(self, module_id: str, stake: float) -> None:
        """Increment trade count and stake used after a trade opens.

        Parameters
        ----------
        module_id:
            Module that opened the trade.
        stake:
            Quote-currency amount committed by the new trade.

        Raises
        ------
        KeyError
            If ``module_id`` is not registered.
        ValueError
            If ``stake`` is negative.
        """
        if stake < 0:
            raise ValueError(f"stake must be >= 0, got {stake}")
        with self._lock:
            budget = self._get_budget_or_raise(module_id)
            budget.current_open_trades += 1
            budget.current_stake_used += stake
            logger.debug(
                "record_open '%s': open_trades=%d, stake_used=%.4f",
                module_id,
                budget.current_open_trades,
                budget.current_stake_used,
            )

    def record_close(self, module_id: str, stake: float) -> None:
        """Decrement trade count and stake used after a trade closes.

        Counters are clamped to zero to guard against mismatched
        record_open / record_close calls (e.g., on bot restart where
        history may be incomplete).

        Parameters
        ----------
        module_id:
            Module whose trade just closed.
        stake:
            Original committed stake being released.

        Raises
        ------
        KeyError
            If ``module_id`` is not registered.
        ValueError
            If ``stake`` is negative.
        """
        if stake < 0:
            raise ValueError(f"stake must be >= 0, got {stake}")
        with self._lock:
            budget = self._get_budget_or_raise(module_id)
            budget.current_open_trades = max(0, budget.current_open_trades - 1)
            budget.current_stake_used = max(0.0, budget.current_stake_used - stake)
            logger.debug(
                "record_close '%s': open_trades=%d, stake_used=%.4f",
                module_id,
                budget.current_open_trades,
                budget.current_stake_used,
            )

    # ------------------------------------------------------------------
    # Suspension / resumption
    # ------------------------------------------------------------------

    def record_suspension(self, module_id: str) -> None:
        """Mark a module as suspended — no new trades allowed.

        Existing open trades are unaffected; their stake remains accounted
        for.  Idempotent: suspending an already-suspended module is a no-op
        (logged at DEBUG level).

        Raises
        ------
        KeyError
            If ``module_id`` is not registered.
        """
        with self._lock:
            budget = self._get_budget_or_raise(module_id)
            if budget.is_suspended:
                logger.debug(
                    "record_suspension: '%s' is already suspended — no-op",
                    module_id,
                )
                return
            budget.is_suspended = True
            logger.warning(
                "Module '%s' suspended: open_trades=%d, stake_used=%.4f",
                module_id,
                budget.current_open_trades,
                budget.current_stake_used,
            )

    def record_resumption(self, module_id: str) -> None:
        """Lift suspension from a module, allowing new trades again.

        Idempotent: resuming an active (non-suspended) module is a no-op.

        Raises
        ------
        KeyError
            If ``module_id`` is not registered.
        """
        with self._lock:
            budget = self._get_budget_or_raise(module_id)
            if not budget.is_suspended:
                logger.debug(
                    "record_resumption: '%s' is not suspended — no-op",
                    module_id,
                )
                return
            budget.is_suspended = False
            logger.info(
                "Module '%s' resumed: open_trades=%d, stake_used=%.4f",
                module_id,
                budget.current_open_trades,
                budget.current_stake_used,
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_utilization(self, module_id: str) -> Dict[str, Any]:
        """Return a snapshot of current utilization for one module.

        Intended for logging, dashboards, and the observability layer.
        The returned dict is a plain copy — mutating it has no effect on
        internal state.

        Parameters
        ----------
        module_id:
            Module to query.

        Returns
        -------
        dict with keys:
            ``module_id``, ``max_open_trades``, ``current_open_trades``,
            ``trade_slots_free``, ``stake_allocation_pct``,
            ``current_stake_used``, ``is_suspended``.
            ``allocated_max_abs`` is intentionally excluded because total
            capital is not stored here; the caller can compute it.

        Raises
        ------
        KeyError
            If ``module_id`` is not registered.
        """
        with self._lock:
            budget = self._get_budget_or_raise(module_id)
            return {
                "module_id": budget.module_id,
                "max_open_trades": budget.max_open_trades,
                "current_open_trades": budget.current_open_trades,
                "trade_slots_free": budget.max_open_trades - budget.current_open_trades,
                "stake_allocation_pct": budget.stake_allocation_pct,
                "current_stake_used": budget.current_stake_used,
                "is_suspended": budget.is_suspended,
            }

    def get_all_budgets(self) -> Dict[str, ModuleBudget]:
        """Return a deep copy of the full budget registry.

        Each ``ModuleBudget`` value is an independent copy — callers may
        safely read and inspect the returned objects without risk of
        accidentally mutating live budget counters.

        Returns
        -------
        Dict mapping module_id -> ModuleBudget copy for every registered module.
        """
        with self._lock:
            return {mid: copy.copy(budget) for mid, budget in self._budgets.items()}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_budget_or_raise(self, module_id: str) -> ModuleBudget:
        """Retrieve a budget entry, raising ``KeyError`` if absent.

        Must be called while ``self._lock`` is held.
        """
        budget = self._budgets.get(module_id)
        if budget is None:
            raise KeyError(
                f"Module '{module_id}' is not registered with BudgetAllocator. "
                "Call register_module() first."
            )
        return budget

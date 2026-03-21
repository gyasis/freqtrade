"""
orchestrator/algo_lifecycle.py
==============================
T008 — LATS Algo System

Per-pair state machine for module lifecycle management.

Governs state transitions:
  INACTIVE → ACTIVE → DRAINING → SWITCHING → ACTIVE
                │           │           │
                ▼           ▼           ▼
            SUSPENDED   SUSPENDED   SUSPENDED
                │
                ▼
            INACTIVE

Design notes
------------
- State is tracked per *(module_id, pair)* combination, not globally per
  module.  A module may be ACTIVE on BTC/USDT while still INACTIVE on
  ETH/USDT.
- All mutation methods are guarded by a ``threading.Lock`` so the state
  machine is safe for use in multi-threaded freqtrade workers.
- Transition validation is delegated to :data:`VALID_TRANSITIONS` from the
  base module — this class never hard-codes transition logic itself.
- Failure counts are *consecutive* failures.  A successful
  :meth:`transition` call resets the count to zero for that
  (module_id, pair).
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List

from ..base.ialgo_module import MANAGED_STATES, VALID_TRANSITIONS, ModuleState

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("algo_system.lifecycle")


# ---------------------------------------------------------------------------
# AlgoLifecycleSM
# ---------------------------------------------------------------------------


class AlgoLifecycleSM:
    """
    Manages module lifecycle state per (module_id, pair) combination.

    Internal structure
    ------------------
    ``_states``
        ``Dict[str, Dict[str, ModuleState]]``
        Outer key is *module_id*, inner key is *pair*.  Missing entries
        imply :attr:`ModuleState.INACTIVE` — callers do not need to
        pre-register every pair.

    ``_failure_counts``
        ``Dict[str, Dict[str, int]]``
        Outer key is *module_id*, inner key is *pair*.  Counts consecutive
        health-check / candle-processing failures since the last successful
        :meth:`transition`.

    All public methods that mutate state acquire ``self._lock`` before
    reading or writing, making the instance safe to share across threads.

    Example
    -------
    >>> sm = AlgoLifecycleSM()
    >>> sm.register_module("momentum_v1")
    >>> sm.transition("momentum_v1", "BTC/USDT", ModuleState.ACTIVE)
    True
    >>> sm.get_state("momentum_v1", "BTC/USDT")
    <ModuleState.ACTIVE: 'active'>
    >>> sm.is_managed("momentum_v1", "BTC/USDT")
    True
    """

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, ModuleState]] = {}
        self._failure_counts: Dict[str, Dict[str, int]] = {}
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_module(self, module_id: str) -> None:
        """
        Initialise tracking dicts for a new module.

        No-op if *module_id* is already registered.  This method is
        idempotent and safe to call multiple times.

        Parameters
        ----------
        module_id:
            Unique identifier for the module (e.g. ``"momentum_v1"``).
        """
        with self._lock:
            if module_id not in self._states:
                self._states[module_id] = {}
                self._failure_counts[module_id] = {}
                logger.debug("Registered module '%s' in lifecycle state machine.", module_id)

    def deregister_pair(self, module_id: str, pair: str) -> None:
        """
        Remove all tracking for *(module_id, pair)*.

        Used on clean shutdown or when a pair is permanently removed from
        the pair list.  Safe to call even if the pair was never registered.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol (e.g. ``"BTC/USDT"``).
        """
        with self._lock:
            if module_id in self._states:
                removed_state = self._states[module_id].pop(pair, None)
                if removed_state is not None:
                    logger.debug(
                        "Deregistered pair '%s' from module '%s' (was %s).",
                        pair,
                        module_id,
                        removed_state.value,
                    )
            if module_id in self._failure_counts:
                self._failure_counts[module_id].pop(pair, None)

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_state(self, module_id: str, pair: str) -> ModuleState:
        """
        Return the current lifecycle state for *(module_id, pair)*.

        Defaults to :attr:`ModuleState.INACTIVE` if the combination has
        never been registered or has been deregistered.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.

        Returns
        -------
        ModuleState
            Current state — never ``None``.
        """
        with self._lock:
            return self._states.get(module_id, {}).get(pair, ModuleState.INACTIVE)

    # ------------------------------------------------------------------
    # Transition
    # ------------------------------------------------------------------

    def transition(self, module_id: str, pair: str, new_state: ModuleState) -> bool:
        """
        Attempt a state transition for *(module_id, pair)*.

        Validates the requested transition against :data:`VALID_TRANSITIONS`.
        If the transition is valid:
          - Updates the stored state.
          - Resets the consecutive failure count to ``0``.
          - Logs the transition at DEBUG level.

        If the transition is *invalid*:
          - Leaves the stored state unchanged.
          - Logs the rejection at DEBUG level.
          - Returns ``False``.

        A transition from a state to *itself* is always rejected (it is
        not listed in :data:`VALID_TRANSITIONS`).

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.
        new_state:
            The desired :class:`ModuleState` to transition into.

        Returns
        -------
        bool
            ``True`` if the transition was applied, ``False`` otherwise.
        """
        with self._lock:
            # Auto-register module if not yet known.
            if module_id not in self._states:
                self._states[module_id] = {}
                self._failure_counts[module_id] = {}

            current_state = self._states[module_id].get(pair, ModuleState.INACTIVE)

            allowed: set = VALID_TRANSITIONS.get(current_state, set())
            if new_state not in allowed:
                logger.debug(
                    "REJECTED transition for module='%s' pair='%s': %s → %s "
                    "(allowed: %s).",
                    module_id,
                    pair,
                    current_state.value,
                    new_state.value,
                    {s.value for s in allowed},
                )
                return False

            # Apply the transition.
            self._states[module_id][pair] = new_state
            # Reset failure count on any successful transition.
            self._failure_counts[module_id][pair] = 0

            logger.debug(
                "Transition for module='%s' pair='%s': %s → %s.",
                module_id,
                pair,
                current_state.value,
                new_state.value,
            )
            return True

    # ------------------------------------------------------------------
    # Failure tracking
    # ------------------------------------------------------------------

    def record_failure(self, module_id: str, pair: str) -> int:
        """
        Increment the consecutive failure count for *(module_id, pair)*.

        Auto-registers the module/pair if not yet tracked.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.

        Returns
        -------
        int
            The *new* consecutive failure count after incrementing.
        """
        with self._lock:
            if module_id not in self._failure_counts:
                self._failure_counts[module_id] = {}
            current = self._failure_counts[module_id].get(pair, 0)
            new_count = current + 1
            self._failure_counts[module_id][pair] = new_count
            logger.debug(
                "Failure recorded for module='%s' pair='%s': consecutive_failures=%d.",
                module_id,
                pair,
                new_count,
            )
            return new_count

    def reset_failure_count(self, module_id: str, pair: str) -> None:
        """
        Reset the consecutive failure count to ``0`` for *(module_id, pair)*.

        Called on a successful candle processing run to clear any previously
        accumulated failure streak.  Safe to call even if no failures have
        been recorded.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.
        """
        with self._lock:
            if module_id in self._failure_counts:
                if self._failure_counts[module_id].get(pair, 0) != 0:
                    self._failure_counts[module_id][pair] = 0
                    logger.debug(
                        "Failure count reset for module='%s' pair='%s'.",
                        module_id,
                        pair,
                    )

    def get_failure_count(self, module_id: str, pair: str) -> int:
        """
        Return the current consecutive failure count for *(module_id, pair)*.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.

        Returns
        -------
        int
            Number of consecutive failures.  ``0`` if no failures have been
            recorded or if the pair has never been tracked.
        """
        with self._lock:
            return self._failure_counts.get(module_id, {}).get(pair, 0)

    # ------------------------------------------------------------------
    # Managed-state helpers
    # ------------------------------------------------------------------

    def is_managed(self, module_id: str, pair: str) -> bool:
        """
        Return ``True`` if the module/pair is in a :data:`MANAGED_STATES` state.

        A "managed" pair requires active order management: position sizing,
        stoploss checks, and exit signal routing.

        :data:`MANAGED_STATES` contains :attr:`ModuleState.ACTIVE`,
        :attr:`ModuleState.DRAINING`, and :attr:`ModuleState.SWITCHING`.

        Parameters
        ----------
        module_id:
            Module identifier.
        pair:
            Trading pair symbol.

        Returns
        -------
        bool
        """
        with self._lock:
            state = self._states.get(module_id, {}).get(pair, ModuleState.INACTIVE)
            return state in MANAGED_STATES

    # ------------------------------------------------------------------
    # Bulk queries
    # ------------------------------------------------------------------

    def get_all_pairs_for_module(self, module_id: str) -> Dict[str, ModuleState]:
        """
        Return a *copy* of all pair → state entries for *module_id*.

        Returns an empty dict if the module has never been registered or has
        no tracked pairs.

        Parameters
        ----------
        module_id:
            Module identifier.

        Returns
        -------
        Dict[str, ModuleState]
            Shallow copy so that callers cannot accidentally mutate internal
            state.
        """
        with self._lock:
            return dict(self._states.get(module_id, {}))

    def get_all_managed_pairs(self, module_id: str) -> List[str]:
        """
        Return the list of pairs where *module_id* is in a managed state.

        "Managed" means the state is one of :data:`MANAGED_STATES` (ACTIVE,
        DRAINING, or SWITCHING).

        Parameters
        ----------
        module_id:
            Module identifier.

        Returns
        -------
        List[str]
            Pair symbols.  Order is unspecified but deterministic within a
            single Python process.
        """
        with self._lock:
            pairs = self._states.get(module_id, {})
            return [pair for pair, state in pairs.items() if state in MANAGED_STATES]

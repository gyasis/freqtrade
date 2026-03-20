"""
orchestrator/signal_arbiter.py
Resolves conflicting module signals using priority hierarchy.

Priority order (highest first):
1. RoutingDecision authoritative_module_id (reasoning engine is the authority)
2. Module confidence score (ModuleSignal.confidence)
3. Registration order (priority_order list index)
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ..base.ialgo_module import ModuleSignal
    from ..reasoning.reasoning_interface import RoutingDecision

logger = logging.getLogger("algo_system.arbiter")


class SignalArbiter:
    def __init__(self, priority_order: List[str]):
        self._priority_order: List[str] = list(priority_order)
        self._lock = threading.Lock()
        self._last_winner: Optional[str] = None

    def register_module(self, module_id: str) -> None:
        with self._lock:
            if module_id not in self._priority_order:
                self._priority_order.append(module_id)

    def _get_priority_rank(self, module_id: str) -> int:
        try:
            return self._priority_order.index(module_id)
        except ValueError:
            return len(self._priority_order)

    def arbitrate(
        self,
        signals: Dict[str, "ModuleSignal"],
        routing_decision: Optional["RoutingDecision"] = None,
    ) -> Tuple[Optional[str], Optional["ModuleSignal"]]:
        """Select winning signal. Returns (module_id, signal) or (None, None)."""
        with self._lock:
            # Filter to modules that actually have a signal
            active = {
                mid: sig for mid, sig in signals.items()
                if sig is not None and (sig.has_entry_signal() or sig.has_exit_signal())
            }
            if not active:
                return None, None

            winner_id: Optional[str] = None
            win_reason: str = ""

            # Step 1: routing decision authority
            if routing_decision is not None and not routing_decision.is_expired():
                auth = routing_decision.authoritative_module_id
                if auth in active:
                    winner_id = auth
                    win_reason = f"routing authority (confidence={routing_decision.confidence:.2f})"
                elif routing_decision.fallback_module_id and routing_decision.fallback_module_id in active:
                    winner_id = routing_decision.fallback_module_id
                    win_reason = "routing fallback"

            # Step 2: highest confidence + priority rank tiebreak
            if winner_id is None:
                best = sorted(
                    active.items(),
                    key=lambda kv: (-kv[1].confidence, self._get_priority_rank(kv[0])),
                )
                winner_id, _ = best[0]
                win_reason = f"confidence={active[winner_id].confidence:.2f}"

            losers = [mid for mid in active if mid != winner_id]
            logger.debug(
                "arbiter: %s wins (%s) over %s", winner_id, win_reason, losers
            )

            if winner_id != self._last_winner:
                logger.info(
                    "arbiter: authority changed from %s to %s",
                    self._last_winner,
                    winner_id,
                )
                self._last_winner = winner_id

            return winner_id, active[winner_id]

    def get_priority_order(self) -> List[str]:
        with self._lock:
            return list(self._priority_order)

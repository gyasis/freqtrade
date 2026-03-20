"""
reasoning/reasoning_interface.py
IReasoningEngine ABC + RoutingDecision dataclass.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from pandas import DataFrame

logger = logging.getLogger("algo_system.reasoning")


@dataclass
class RoutingDecision:
    authoritative_module_id: str
    confidence: float
    rationale: str
    valid_for_candles: int
    candles_remaining: int
    fallback_module_id: Optional[str] = None

    def is_expired(self) -> bool:
        return self.candles_remaining <= 0

    def tick(self) -> None:
        self.candles_remaining = max(0, self.candles_remaining - 1)

    def to_dict(self) -> dict:
        return {
            "authoritative_module_id": self.authoritative_module_id,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "valid_for_candles": self.valid_for_candles,
            "candles_remaining": self.candles_remaining,
            "fallback_module_id": self.fallback_module_id,
        }


class IReasoningEngine(ABC):

    @abstractmethod
    def initialize(self, config: dict, active_module_ids: List[str]) -> None:
        """Called once at bot start."""

    @abstractmethod
    def score_modules(
        self, pair: str, df: DataFrame, active_module_ids: List[str]
    ) -> Dict[str, float]:
        """Return Dict[module_id, score 0.0-1.0] for current market conditions."""

    @abstractmethod
    def get_routing_decision(
        self,
        pair: str,
        df: DataFrame,
        active_module_ids: List[str],
        current_decision: Optional[RoutingDecision] = None,
    ) -> Optional[RoutingDecision]:
        """Return authoritative module or None if no module qualifies."""

    @abstractmethod
    def shutdown(self) -> None:
        """Clean up. Must not raise."""

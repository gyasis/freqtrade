"""
reasoning/rule_based_reasoning.py
Rule-based reasoning engine using talib ADX + qtpylib Bollinger Bands.
No ML training required — works on day one.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pandas import DataFrame

from .reasoning_interface import IReasoningEngine, RoutingDecision

logger = logging.getLogger("algo_system.reasoning.rule_based")


class RuleBasedReasoningEngine(IReasoningEngine):
    """
    Scores modules using market regime indicators:
    - ADX < threshold → ranging market → grid module scores high
    - BB width < threshold → low volatility → grid module scores higher
    - RSI 40-60 → neutral momentum → grid module scores higher

    Scoring for grid_trading_v1 (default module):
      +0.6  if ADX < adx_ranging_threshold (primary signal)
      +0.25 if bb_width < bb_width_threshold (secondary signal)
      +0.15 if 40 < RSI < 60 (confirming signal)
      Score clamped to [0.0, 1.0]

    Any other module gets score 0.0 (no rule for it = no opinion).
    """

    def __init__(self) -> None:
        self._config: dict = {}
        self._active_module_ids: List[str] = []
        self._adx_threshold: float = 25.0
        self._bb_width_threshold: float = 0.04
        self._activation_threshold: float = 0.6
        self._ttl_candles: int = 10

    def initialize(self, config: dict, active_module_ids: List[str]) -> None:
        self._config = config
        self._active_module_ids = list(active_module_ids)
        self._adx_threshold = config.get("adx_ranging_threshold", 25.0)
        self._bb_width_threshold = config.get("bb_width_ranging_threshold", 0.04)
        self._activation_threshold = config.get("activation_threshold", 0.6)
        self._ttl_candles = config.get("routing_ttl_candles", 10)
        logger.info(
            "RuleBasedReasoningEngine initialized: ADX<%s BB_width<%s activation>%s",
            self._adx_threshold, self._bb_width_threshold, self._activation_threshold,
        )

    def score_modules(
        self, pair: str, df: DataFrame, active_module_ids: List[str]
    ) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for mid in active_module_ids:
            if mid == "grid_trading_v1":
                scores[mid] = self._score_grid(df)
            else:
                scores[mid] = 0.0
        logger.debug("Scores for %s: %s", pair, scores)
        return scores

    def _score_grid(self, df: DataFrame) -> float:
        if len(df) < 20:
            return 0.0
        score = 0.0
        try:
            import talib
            adx = talib.ADX(df["high"], df["low"], df["close"], timeperiod=14)
            adx_val = adx.iloc[-1]
            if adx_val < self._adx_threshold:
                score += 0.6
        except Exception:
            pass  # talib unavailable — skip this component

        try:
            import talib
            upper, mid, lower = talib.BBANDS(df["close"], timeperiod=20)
            mid_val = mid.iloc[-1]
            if mid_val and mid_val != 0:
                bb_width = (upper.iloc[-1] - lower.iloc[-1]) / mid_val
                if bb_width < self._bb_width_threshold:
                    score += 0.25
        except Exception:
            pass

        try:
            import talib
            rsi = talib.RSI(df["close"], timeperiod=14)
            rsi_val = rsi.iloc[-1]
            if 40 < rsi_val < 60:
                score += 0.15
        except Exception:
            pass

        return min(1.0, score)

    def get_routing_decision(
        self,
        pair: str,
        df: DataFrame,
        active_module_ids: List[str],
        current_decision: Optional[RoutingDecision] = None,
    ) -> Optional[RoutingDecision]:
        # Return existing decision if still valid
        if current_decision is not None and not current_decision.is_expired():
            current_decision.tick()
            return current_decision

        scores = self.score_modules(pair, df, active_module_ids)
        if not scores:
            return None

        best_id = max(scores, key=lambda k: scores[k])
        best_score = scores[best_id]

        if best_score < self._activation_threshold:
            logger.debug(
                "No module qualifies for %s (best=%s score=%.2f < threshold=%.2f)",
                pair, best_id, best_score, self._activation_threshold,
            )
            return None

        rationale = (
            f"rule_based: {best_id} selected for {pair} "
            f"(score={best_score:.2f}, ADX_threshold={self._adx_threshold})"
        )
        logger.info(rationale)

        return RoutingDecision(
            authoritative_module_id=best_id,
            confidence=best_score,
            rationale=rationale,
            valid_for_candles=self._ttl_candles,
            candles_remaining=self._ttl_candles,
        )

    def shutdown(self) -> None:
        logger.debug("RuleBasedReasoningEngine shutdown")

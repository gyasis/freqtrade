"""
reasoning/entry_quality_evaluator.py
Scores entry quality for grid initialization gating.
Applied ONLY when opening a new grid — never to an already-active grid.
"""
from __future__ import annotations

import logging
from typing import Tuple

from pandas import DataFrame

logger = logging.getLogger("algo_system.entry_quality")


class EntryQualityEvaluator:
    """
    Scores 0.0-1.0 whether the current price is a favorable grid entry point.

    Scoring components:
      +0.5  if price is within mid_pct of the N-candle range midpoint (mean-reversion signal)
      +0.3  if abs(N-candle momentum) < momentum_threshold (not trending strongly)
      +0.2  if RSI is between 40 and 60 (neutral momentum confirmation)
    """

    def __init__(self, config: dict) -> None:
        self._lookback = config.get("momentum_lookback_candles", 10)
        self._momentum_threshold = config.get("momentum_threshold", 0.03)
        self._quality_threshold = config.get("entry_quality_threshold", 0.5)
        self._max_defer = config.get("max_defer_candles", 20)

    def score(self, pair: str, df: DataFrame) -> Tuple[float, str]:
        """
        Returns (score: float, rationale: str).
        score 0.0-1.0. rationale is human-readable.
        """
        if len(df) < self._lookback + 1:
            return 0.0, "insufficient_data"

        score = 0.0
        reasons = []
        close = df["close"]
        current = close.iloc[-1]
        past = close.iloc[-self._lookback - 1]

        # Momentum check (+-momentum_threshold over lookback)
        if past != 0:
            momentum = abs((current - past) / past)
            if momentum < self._momentum_threshold:
                score += 0.3
                reasons.append(f"low_momentum={momentum:.3f}")
            else:
                reasons.append(f"high_momentum={momentum:.3f}")

        # Mean-reversion: price near midpoint of N-candle range
        high_n = df["high"].iloc[-self._lookback:].max()
        low_n = df["low"].iloc[-self._lookback:].min()
        if high_n > low_n:
            midpoint = (high_n + low_n) / 2
            distance_from_mid = abs(current - midpoint) / (high_n - low_n)
            if distance_from_mid < 0.3:  # within 30% of range from center
                score += 0.5
                reasons.append(f"near_midpoint(dist={distance_from_mid:.2f})")
            else:
                reasons.append(f"far_from_midpoint(dist={distance_from_mid:.2f})")

        # RSI confirmation
        try:
            import talib
            rsi = talib.RSI(close, timeperiod=14)
            rsi_val = rsi.iloc[-1]
            if 40 < rsi_val < 60:
                score += 0.2
                reasons.append(f"neutral_rsi={rsi_val:.1f}")
        except Exception:
            pass

        score = min(1.0, score)
        rationale = f"{pair} entry_quality={score:.2f} [{', '.join(reasons)}]"
        return score, rationale

    def is_quality_entry(self, pair: str, df: DataFrame, defer_count: int) -> Tuple[bool, str]:
        """
        Returns (should_enter: bool, rationale: str).
        Also warns if defer_count exceeds max_defer.
        """
        score, rationale = self.score(pair, df)
        if defer_count >= self._max_defer:
            logger.warning(
                "Entry deferred %d/%d candles for %s — check market conditions",
                defer_count, self._max_defer, pair,
            )
        return score >= self._quality_threshold, rationale

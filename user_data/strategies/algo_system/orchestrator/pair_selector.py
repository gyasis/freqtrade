"""
orchestrator/pair_selector.py
Dynamic per-module pair filtering based on market characteristics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

from pandas import DataFrame

logger = logging.getLogger("algo_system.pair_selector")


@dataclass
class PairSelectorCriteria:
    max_adx: float = 30.0          # exclude pairs trending above this ADX
    min_bb_width: float = 0.005    # exclude pairs with BB width below this (too flat)
    max_bb_width: float = 0.20     # exclude pairs with BB width above this (too volatile)
    min_daily_volume: float = 0.0  # min 24h volume in stake currency (0 = disabled)


class PairSelector:
    """
    Evaluates candidate pairs against per-module criteria.
    Produces an active pair list per module, re-evaluated every N candles.
    """

    def __init__(self, config: dict) -> None:
        self._enabled: bool = config.get("enabled", True)
        self._eval_interval: int = config.get("evaluation_interval_candles", 24)
        self._criteria_raw: Dict[str, dict] = config.get("criteria", {})
        self._criteria: Dict[str, PairSelectorCriteria] = {}
        self._active_pairs: Dict[str, List[str]] = {}  # module_id -> pairs
        self._candle_counter: int = 0
        self._talib_warned: bool = False  # log talib absence exactly once

        for mid, crit_dict in self._criteria_raw.items():
            self._criteria[mid] = PairSelectorCriteria(**{
                k: v for k, v in crit_dict.items()
                if k in PairSelectorCriteria.__dataclass_fields__
            })

    def evaluate_pair(
        self, _pair: str, df: DataFrame, criteria: PairSelectorCriteria
    ) -> Tuple[bool, str]:
        """
        Returns (qualifies: bool, reason: str).
        """
        if len(df) < 20:
            return False, "insufficient_data"

        reasons = []
        try:
            import talib
            adx = talib.ADX(df["high"], df["low"], df["close"], timeperiod=14)
            adx_val = adx.iloc[-1]
            if adx_val > criteria.max_adx:
                return False, f"trending(ADX={adx_val:.1f}>{criteria.max_adx})"
            reasons.append(f"ADX={adx_val:.1f}")
        except ImportError:
            if not self._talib_warned:
                logger.warning(
                    "talib not installed — ADX pair filter disabled. "
                    "Install TA-Lib to enable trend-based pair selection."
                )
                self._talib_warned = True
        except Exception as exc:
            logger.debug("ADX calculation failed: %s", exc)

        try:
            import talib
            upper, mid, lower = talib.BBANDS(df["close"], timeperiod=20)
            mid_val = mid.iloc[-1]
            if mid_val and mid_val != 0:
                bb_width = (upper.iloc[-1] - lower.iloc[-1]) / mid_val
                if bb_width < criteria.min_bb_width:
                    return False, f"too_flat(bb_width={bb_width:.4f}<{criteria.min_bb_width})"
                if bb_width > criteria.max_bb_width:
                    return False, f"too_volatile(bb_width={bb_width:.4f}>{criteria.max_bb_width})"
                reasons.append(f"bb_width={bb_width:.4f}")
        except ImportError:
            pass  # already warned above on the ADX block
        except Exception as exc:
            logger.debug("BBANDS calculation failed: %s", exc)

        return True, f"qualifies [{', '.join(reasons)}]"

    def tick(self) -> None:
        """Call every candle to track evaluation interval."""
        self._candle_counter += 1

    def should_evaluate(self) -> bool:
        return self._candle_counter % self._eval_interval == 0

    def update_active_pairs(
        self,
        module_id: str,
        whitelist: List[str],
        df_map: Dict[str, DataFrame],
    ) -> Tuple[List[str], List[str]]:
        """
        Re-evaluate pairs for module. Returns (added, removed) pair lists.
        removed pairs with open positions must be handled by caller (draining).
        """
        if not self._enabled:
            return [], []

        criteria = self._criteria.get(module_id, PairSelectorCriteria())
        new_active = []
        for pair in whitelist:
            df = df_map.get(pair)
            if df is None:
                continue
            qualifies, reason = self.evaluate_pair(pair, df, criteria)
            logger.debug("PairSelector %s %s: %s", module_id, pair, reason)
            if qualifies:
                new_active.append(pair)

        prev = set(self._active_pairs.get(module_id, []))
        curr = set(new_active)
        added = sorted(curr - prev)
        removed = sorted(prev - curr)

        if added:
            logger.info("PairSelector: added to %s: %s", module_id, added)
        if removed:
            logger.info(
                "PairSelector: removed from %s: %s (check for open positions)",
                module_id, removed,
            )

        self._active_pairs[module_id] = new_active
        return added, removed

    def get_active_pairs(self, module_id: str) -> List[str]:
        return list(self._active_pairs.get(module_id, []))

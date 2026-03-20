"""
modules/grid_trading/grid_calculator.py
Stateless pure-math calculator for grid trading.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Set

logger = logging.getLogger("algo_system.grid_calculator")


class GridCalculator:
    """Stateless calculator — all methods are pure @staticmethod functions."""

    @staticmethod
    def validate_grid(lower: float, upper: float, count: int) -> None:
        if lower <= 0 or upper <= 0:
            raise ValueError(f"Grid bounds must be positive: lower={lower}, upper={upper}")
        if lower >= upper:
            raise ValueError(f"lower ({lower}) must be < upper ({upper})")
        if count < 2:
            raise ValueError(f"grid_count must be >= 2, got {count}")

    @staticmethod
    def calculate_levels(lower: float, upper: float, count: int) -> List[float]:
        GridCalculator.validate_grid(lower, upper, count)
        return [lower + (upper - lower) * i / (count - 1) for i in range(count)]

    @staticmethod
    def get_crossed_levels_down(
        from_price: float,
        to_price: float,
        levels: List[float],
    ) -> List[float]:
        """Levels crossed downward (buy triggers): to_price <= level < from_price."""
        if from_price <= to_price:
            return []
        crossed = [lv for lv in levels if to_price <= lv < from_price]
        return sorted(crossed, reverse=True)

    @staticmethod
    def get_crossed_levels_up(
        from_price: float,
        to_price: float,
        levels: List[float],
        filled_levels: Set[float],
    ) -> List[float]:
        """Filled levels crossed upward (sell triggers): from_price <= level < to_price AND filled."""
        if to_price <= from_price:
            return []
        crossed = [lv for lv in levels if from_price <= lv < to_price and lv in filled_levels]
        return sorted(crossed)

    @staticmethod
    def get_nearest_level(price: float, levels: List[float]) -> Optional[float]:
        if not levels:
            return None
        return min(levels, key=lambda lv: abs(lv - price))

    @staticmethod
    def get_level_below(price: float, levels: List[float]) -> Optional[float]:
        below = [lv for lv in levels if lv < price]
        return max(below) if below else None

    @staticmethod
    def get_level_above(price: float, levels: List[float]) -> Optional[float]:
        above = [lv for lv in levels if lv > price]
        return min(above) if above else None

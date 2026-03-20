"""
modules/grid_trading/grid_state.py
Per-pair mutable state for GridTradingModule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

logger = logging.getLogger("algo_system.grid_state")


@dataclass
class GridState:
    pair: str
    upper_bound: float
    lower_bound: float
    grid_count: int
    grid_levels: List[float]
    filled_levels: Set[float]
    initial_entry_price: Optional[float] = None
    created_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(
                f"upper_bound ({self.upper_bound}) must be > lower_bound ({self.lower_bound})"
            )
        if self.grid_count < 2:
            raise ValueError(f"grid_count must be >= 2, got {self.grid_count}")
        if len(self.grid_levels) != self.grid_count:
            raise ValueError(
                f"grid_levels length ({len(self.grid_levels)}) must equal grid_count ({self.grid_count})"
            )
        invalid = self.filled_levels - set(self.grid_levels)
        if invalid:
            raise ValueError(f"filled_levels contains prices not in grid_levels: {invalid}")

    def is_level_filled(self, level: float) -> bool:
        return level in self.filled_levels

    def mark_filled(self, level: float) -> None:
        self.filled_levels.add(level)
        self.last_updated = datetime.now(timezone.utc)

    def mark_unfilled(self, level: float) -> None:
        self.filled_levels.discard(level)
        self.last_updated = datetime.now(timezone.utc)

    def is_in_range(self, price: float) -> bool:
        return self.lower_bound <= price <= self.upper_bound

    def is_active(self) -> bool:
        return self.initial_entry_price is not None

    def get_unfilled_levels(self) -> List[float]:
        return sorted(lv for lv in self.grid_levels if lv not in self.filled_levels)

    def get_filled_levels_sorted(self) -> List[float]:
        return sorted(self.filled_levels)

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "upper_bound": self.upper_bound,
            "lower_bound": self.lower_bound,
            "grid_count": self.grid_count,
            "grid_levels": self.grid_levels,
            "filled_levels": sorted(self.filled_levels),
            "initial_entry_price": self.initial_entry_price,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GridState:
        created_at = (
            datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
        )
        last_updated = (
            datetime.fromisoformat(data["last_updated"]) if data.get("last_updated") else None
        )
        return cls(
            pair=data["pair"],
            upper_bound=data["upper_bound"],
            lower_bound=data["lower_bound"],
            grid_count=data["grid_count"],
            grid_levels=data["grid_levels"],
            filled_levels=set(data.get("filled_levels", [])),
            initial_entry_price=data.get("initial_entry_price"),
            created_at=created_at,
            last_updated=last_updated,
        )

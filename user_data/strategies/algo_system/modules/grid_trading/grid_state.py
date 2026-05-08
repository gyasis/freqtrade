"""
modules/grid_trading/grid_state.py
==================================
Per-pair mutable state for ``GridTradingModule``.

Changes vs the pre-003 schema:
  * ``last_price`` is now an explicit field (was a separate dict on the
    module). Required by FR-010 / FR-013 so ``is_active()`` reflects the
    "post-first-candle" state.
  * ``grid_count`` is a derived ``@property`` (``len(grid_levels)``) instead
    of a stored field. Eliminates the redundant invariant
    ``len(grid_levels) == grid_count``.
  * ``is_active()`` flipped to ``last_price is not None`` (was
    ``initial_entry_price is not None``). ``initial_entry_price`` is set at
    grid build time but ``last_price`` is only set on the first candle
    update — the new semantics correctly distinguish "grid built" from
    "grid running".
  * ``from_dict`` is backward-compat for legacy JSON: it discards an
    incoming ``grid_count`` key (now derived) and falls back to
    ``initial_entry_price`` for ``last_price`` when the old key is absent
    (logging a migration warning).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

logger = logging.getLogger("algo_system.grid_state")


@dataclass
class GridState:
    pair: str
    upper_bound: float
    lower_bound: float
    grid_levels: List[float]
    filled_levels: Set[float]
    initial_entry_price: Optional[float] = None
    last_price: Optional[float] = None
    created_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.upper_bound <= self.lower_bound:
            raise ValueError(
                f"upper_bound ({self.upper_bound}) must be > lower_bound ({self.lower_bound})"
            )
        if len(self.grid_levels) < 2:
            raise ValueError(
                f"grid_levels must contain >= 2 levels, got {len(self.grid_levels)}"
            )
        invalid = self.filled_levels - set(self.grid_levels)
        if invalid:
            raise ValueError(
                f"filled_levels contains prices not in grid_levels: {invalid}"
            )

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------
    @property
    def grid_count(self) -> int:
        """Number of grid levels — derived, not stored."""
        return len(self.grid_levels)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def update_last_price(self, price: float) -> None:
        """Record the latest observed price and bump ``last_updated``.

        Called by the module on every candle. Once called at least once,
        ``is_active()`` returns ``True``.

        Rejects NaN, inf, non-numeric, and bool values to keep crossing
        detection (``state.last_price > level``) honest — NaN comparisons
        silently return False which would mask all crossings.
        """
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise TypeError(
                f"update_last_price: price must be a number, got "
                f"{price!r} (type {type(price).__name__})"
            )
        if not math.isfinite(price):
            raise ValueError(
                f"update_last_price: price must be finite, got {price!r} "
                "(NaN/inf would silently break crossing detection)"
            )
        self.last_price = float(price)
        self.last_updated = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
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
        """True once ``update_last_price`` has been called at least once.

        Distinguishes "grid built" (``initial_entry_price`` set) from
        "grid running" (at least one candle observed). Crossing detection
        is meaningful only when ``last_price`` is set.
        """
        return self.last_price is not None

    def get_unfilled_levels(self) -> List[float]:
        return sorted(lv for lv in self.grid_levels if lv not in self.filled_levels)

    def get_filled_levels_sorted(self) -> List[float]:
        return sorted(self.filled_levels)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "upper_bound": self.upper_bound,
            "lower_bound": self.lower_bound,
            "grid_levels": list(self.grid_levels),
            "filled_levels": sorted(self.filled_levels),
            "initial_entry_price": self.initial_entry_price,
            "last_price": self.last_price,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GridState:
        """Deserialise from a dict.

        Backward-compat for legacy state created before the 003 unification:
          * An incoming ``grid_count`` key is silently discarded (now derived).
          * If ``last_price`` is missing, fall back to ``initial_entry_price``
            (so a restart of an older state still produces an active grid)
            and log a migration warning at INFO.
        """
        created_at = (
            datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
        )
        last_updated = (
            datetime.fromisoformat(data["last_updated"]) if data.get("last_updated") else None
        )

        if "grid_count" in data:
            logger.debug(
                "GridState.from_dict: discarding legacy 'grid_count' key (now derived)"
            )

        last_price = data.get("last_price")
        if last_price is None and data.get("initial_entry_price") is not None:
            last_price = data["initial_entry_price"]
            logger.info(
                "GridState.from_dict: legacy state for pair %r has no 'last_price'; "
                "falling back to initial_entry_price=%s",
                data.get("pair"),
                last_price,
            )
        # Validate last_price type: a bad serializer could leave a string here,
        # which would then poison every downstream crossing-detection comparison.
        if last_price is not None:
            if isinstance(last_price, bool) or not isinstance(last_price, (int, float)):
                raise TypeError(
                    f"GridState.from_dict: last_price must be a number or None, got "
                    f"{last_price!r} (type {type(last_price).__name__})"
                )
            if not math.isfinite(last_price):
                raise ValueError(
                    f"GridState.from_dict: last_price must be finite, got {last_price!r}"
                )
            last_price = float(last_price)

        return cls(
            pair=data["pair"],
            upper_bound=data["upper_bound"],
            lower_bound=data["lower_bound"],
            grid_levels=list(data["grid_levels"]),
            filled_levels=set(data.get("filled_levels", [])),
            initial_entry_price=data.get("initial_entry_price"),
            last_price=last_price,
            created_at=created_at,
            last_updated=last_updated,
        )

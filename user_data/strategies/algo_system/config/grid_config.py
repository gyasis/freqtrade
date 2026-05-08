"""
config/grid_config.py
=====================
Single source of truth for all Optuna-tunable grid parameters.

Dependency-free (stdlib + dataclasses only) so the config layer, calculator,
optimizer, and tests can import it without pulling in pandas, numpy, or TA-Lib.

GridConfig
----------
Renamed from ``GridConfigV2`` in
``modules/grid_trading/grid_calculator_legacy.py``. Fields are identical;
validation is exposed as an explicit ``validate()`` method that
``__post_init__`` also calls so both construction-time and explicit
validation work the same way.

Constants
---------
``VALID_METHODS``, ``VALID_ALLOCATION_STRATEGIES``, ``VALID_INDICATORS``
are copied verbatim from ``grid_calculator_legacy.py`` and exported so every
consumer can import them from a single location.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field
from typing import List, Literal

logger = logging.getLogger("algo_system.grid_config")


def _is_positive_finite_number(x: object) -> bool:
    """True for ints/floats that are > 0 and finite. Rejects bool, NaN, inf, non-numerics."""
    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(x) and x > 0


def _is_positive_int(x: object) -> bool:
    """True for ints that are > 0. Rejects bool, float, non-numerics."""
    if isinstance(x, bool):
        return False
    return isinstance(x, int) and x > 0


VALID_METHODS: tuple[str, ...] = (
    "market_price", "moving_average", "mid_high_low", "vwap",
)

VALID_ALLOCATION_STRATEGIES: tuple[str, ...] = (
    "fixed_pct", "fixed_shares", "dynamic_grid", "proportional", "volatility",
)

VALID_INDICATORS: tuple[str, ...] = (
    "ATR", "RSI", "BollingerBands", "SMA", "EMA",
    "StdDev", "CyclePeriod", "CyclePhase", "TrendMode",
)


@dataclass
class GridConfig:
    """All Optuna-tunable configuration parameters for the unified grid engine.

    Renamed from ``GridConfigV2`` — fields are identical. Validation is
    exposed as an explicit ``validate()`` method (also called from
    ``__post_init__``) so test suites can validate without reconstructing.
    """

    grid_distance: float = 0.01
    grid_range: float = 0.20
    method: Literal[
        "market_price", "moving_average", "mid_high_low", "vwap"
    ] = "moving_average"
    period: int = 20

    dynamic_midprice: bool = True
    midprice_adjust_period: int = 5

    auto_adjust: bool = False
    auto_set_grid: bool = False
    auto_set_period: bool = False

    allocation_strategy: Literal[
        "fixed_pct", "fixed_shares", "dynamic_grid", "proportional", "volatility"
    ] = "fixed_pct"
    allocation_param: float = 0.30

    volatility_threshold: float = 0.02
    entry_multiplier: float = 2.0
    deviation_threshold: float = 0.50

    selected_indicators: List[str] = field(default_factory=list)
    log_scale: bool = False
    history_lookback: int = 30

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Raise ``ValueError`` on a degenerate config.

        Checks (in order):
          * ``grid_distance > 0``
          * ``grid_range > grid_distance`` (must produce >= 2 levels)
          * ``period > 0``
          * ``method`` in ``VALID_METHODS``
          * ``allocation_strategy`` in ``VALID_ALLOCATION_STRATEGIES``
          * ``selected_indicators`` is a subset of ``VALID_INDICATORS``
        """
        if not _is_positive_finite_number(self.grid_distance):
            raise ValueError(
                f"grid_distance must be a positive finite number, got "
                f"{self.grid_distance!r} (type {type(self.grid_distance).__name__})"
            )
        if not _is_positive_finite_number(self.grid_range):
            raise ValueError(
                f"grid_range must be a positive finite number, got "
                f"{self.grid_range!r} (type {type(self.grid_range).__name__})"
            )
        if self.grid_range <= self.grid_distance:
            raise ValueError(
                f"grid_range ({self.grid_range!r}) must be > grid_distance "
                f"({self.grid_distance!r}) so the grid produces >= 2 levels"
            )
        if not _is_positive_int(self.period):
            raise ValueError(
                f"period must be a positive int, got "
                f"{self.period!r} (type {type(self.period).__name__})"
            )
        if self.method not in VALID_METHODS:
            raise ValueError(
                f"Invalid method {self.method!r}. Choose from {VALID_METHODS}"
            )
        if self.allocation_strategy not in VALID_ALLOCATION_STRATEGIES:
            raise ValueError(
                f"Invalid allocation_strategy {self.allocation_strategy!r}. "
                f"Choose from {VALID_ALLOCATION_STRATEGIES}"
            )
        unknown = [i for i in self.selected_indicators if i not in VALID_INDICATORS]
        if unknown:
            raise ValueError(
                f"Unknown indicator(s): {unknown}. Valid: {VALID_INDICATORS}"
            )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def grid_count(self) -> int:
        """Number of grid levels (approximate, derived from range/distance)."""
        if self.grid_distance <= 0:
            return 0
        return max(1, int(round(self.grid_range / self.grid_distance)))

    def upper_bound(self, midprice: float) -> float:
        """Upper grid boundary given a midprice."""
        return midprice + self.grid_range / 2.0

    def lower_bound(self, midprice: float) -> float:
        """Lower grid boundary given a midprice."""
        return midprice - self.grid_range / 2.0

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise to a JSON-safe dict (for shared_state persistence)."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GridConfig":
        """Deserialise from a dict, ignoring unknown keys (forward-compat)."""
        known = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

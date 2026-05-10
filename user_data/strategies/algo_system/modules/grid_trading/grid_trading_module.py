"""
modules/grid_trading/grid_trading_module.py
===========================================
GridTradingModule — the first concrete IAlgoModule in the LATS system.

Implements a symmetric grid trading strategy. The initial position is opened by
``generate_entry_signal()``, which also lazily *builds the grid* using the
current candle window so that the unified ``GridCalculator`` (and its midprice /
auto-tune machinery) has the data it needs. After the entry, all subsequent
buys and partial sells are driven by ``adjust_position()`` whenever the price
crosses a grid level.

Architecture
------------
- ``GridConfig`` (``algo_system.config.grid_config``) is the single source of
  truth for tunable parameters — set per-pair by the optimizer, or derived
  on-the-fly from the module's bootstrap config when no optimizer config exists.
- ``GridCalculator`` (stateless, ``@staticmethod`` everywhere) handles all
  pure-math operations.
- ``GridState`` (per-pair dataclass) owns mutable bookkeeping including
  ``last_price`` (the previous-candle price used for crossing detection).
- This module owns the lifecycle of all ``GridState`` instances and persists
  them to ``SharedState`` on shutdown / restores them on ``on_bot_start``.

Notes for live trading
----------------------
``supports_live = ModuleCapability.PARTIAL`` — freqtrade's
``position_adjustment_enable`` must be ``true`` in the strategy config, and the
exchange must support partial order fills.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pandas import DataFrame

from ...base.ialgo_module import IAlgoModule, ModuleCapability, ModuleSignal
from ...base.module_context import ModuleContext
from ...config.grid_config import GridConfig
from ...reasoning.entry_quality_evaluator import EntryQualityEvaluator
from ..grid_trading.grid_calculator import GridCalculator
from ..grid_trading.grid_state import GridState

if TYPE_CHECKING:
    pass


logger = logging.getLogger("algo_system.grid_trading_module")


class GridTradingModule(IAlgoModule):
    """Grid trading module for the LATS algorithm orchestration system.

    Subsequent position adjustments (buys at lower grid levels, partial sells
    at upper levels) are managed through ``adjust_position()``.

    Class attributes
    ----------------
    module_id : str
        Unique identifier used as a key in SharedState, log prefixes, and
        entry/exit tag prefixes.
    version : str
        Semantic version. Increment on breaking state-format changes.
    """

    # ------------------------------------------------------------------
    # Class-level identity
    # ------------------------------------------------------------------
    module_id = "grid_trading_v1"
    version = "1.1.0"  # bumped: GridState.last_price + GridConfig integration

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------
    supports_backtest = ModuleCapability.SUPPORTED
    supports_paper = ModuleCapability.SUPPORTED
    supports_live = ModuleCapability.PARTIAL       # position_adjustment_enable required
    supports_hyperopt = ModuleCapability.PARTIAL
    supports_short = ModuleCapability.UNSUPPORTED
    supports_position_adjust = ModuleCapability.SUPPORTED

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------
    def __init__(self) -> None:
        self._grid_states: Dict[str, GridState] = {}      # pair -> GridState
        self._configs: Dict[str, GridConfig] = {}         # pair -> optimizer-provided GridConfig
        self._config: Dict[str, Any] = {}                 # module bootstrap config
        self._defer_counts: Dict[str, int] = {}           # pair -> consecutive defer count
        self._entry_quality_evaluator: EntryQualityEvaluator = EntryQualityEvaluator({})
        self._logger = logging.getLogger(f"algo_system.{self.module_id}")

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------
    def initialize(self, ctx: ModuleContext) -> None:
        """Validate and cache module config from ``SharedState``.

        Bootstrap config keys (used when no optimizer-derived ``GridConfig``
        exists for a pair):
          * ``upper_bound_pct`` — fraction of current_rate above midprice
          * ``lower_bound_pct`` — fraction of current_rate below midprice
          * ``grid_count``     — desired number of levels
          * ``initial_stake``  — total stake split across grid levels
        """
        raw_cfg = ctx.shared_state.get(self.module_id, "config") or {}

        upper_bound_pct = float(raw_cfg.get("upper_bound_pct", 0.05))
        lower_bound_pct = float(raw_cfg.get("lower_bound_pct", 0.05))
        grid_count = int(raw_cfg.get("grid_count", 5))
        initial_stake = float(raw_cfg.get("initial_stake", 100.0))

        if upper_bound_pct <= 0:
            raise ValueError(f"upper_bound_pct must be > 0, got {upper_bound_pct}")
        if lower_bound_pct <= 0:
            raise ValueError(f"lower_bound_pct must be > 0, got {lower_bound_pct}")
        if grid_count < 2:
            raise ValueError(f"grid_count must be >= 2, got {grid_count}")
        if initial_stake <= 0:
            raise ValueError(f"initial_stake must be > 0, got {initial_stake}")

        self._config = {
            "upper_bound_pct": upper_bound_pct,
            "lower_bound_pct": lower_bound_pct,
            "grid_count": grid_count,
            "initial_stake": initial_stake,
        }
        evaluator_cfg = {"entry_quality_threshold": 0.0, **raw_cfg}
        self._entry_quality_evaluator = EntryQualityEvaluator(evaluator_cfg)
        self._logger.info(
            "GridTradingModule initialized (config: %s)", self._config
        )

    def on_bot_start(self, ctx: ModuleContext) -> None:
        """Restore persisted grid states from ``SharedState``."""
        for pair in ctx.data_provider.current_whitelist():
            entry = ctx.shared_state.get(self.module_id, pair)
            if entry and entry.get("data"):
                try:
                    self._grid_states[pair] = GridState.from_dict(entry["data"])
                    self._logger.info("Restored grid state for %s", pair)
                except Exception as exc:
                    self._logger.warning(
                        "Failed to restore grid state for %s: %s — starting fresh",
                        pair, exc,
                    )

    def shutdown(self, ctx: ModuleContext) -> None:
        """Persist all active grid states to ``SharedState``. Never raises."""
        for pair, state in self._grid_states.items():
            try:
                ctx.shared_state.set(self.module_id, pair, state.to_dict())
                self._logger.debug("Persisted grid state for %s", pair)
            except Exception as exc:
                self._logger.error(
                    "Error persisting grid state for %s: %s", pair, exc
                )

    # ------------------------------------------------------------------
    # Per-candle methods
    # ------------------------------------------------------------------
    def populate_indicators(
        self, df: DataFrame, metadata: dict, ctx: ModuleContext
    ) -> DataFrame:
        """Attach the ADX indicator column for entry quality gating."""
        try:
            import talib  # noqa: PLC0415 — optional dependency

            df[f"_{self.module_id}_adx"] = talib.ADX(
                df["high"], df["low"], df["close"], timeperiod=14
            )
        except Exception as exc:
            self._logger.debug(
                "talib ADX unavailable: %s — entry quality gating disabled", exc
            )
        return df

    def generate_entry_signal(
        self, df: DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Emit an entry signal AND lazily build the grid for this pair.

        Lazy initialisation lives here (not in ``adjust_position``) because
        ``df`` is available — the unified ``GridCalculator`` needs candle data
        for VWAP midprice, log-scale levels, and indicator-driven auto-tuning.

        Once a grid exists (regardless of ``is_active`` state), no further
        entry signals are emitted: ``adjust_position`` takes over.
        """
        pair = metadata.get("pair", getattr(ctx, "pair", ""))

        # Already have a grid for this pair — adjust_position will handle subsequent trades
        if pair in self._grid_states:
            return ModuleSignal()

        # Entry quality gating
        defer_count = self._defer_counts.get(pair, 0)
        qualifies, rationale = self._entry_quality_evaluator.is_quality_entry(
            pair, df, defer_count
        )
        if not qualifies:
            self._defer_counts[pair] = defer_count + 1
            self._logger.debug("Entry deferred for %s: %s", pair, rationale)
            return ModuleSignal()

        self._defer_counts[pair] = 0

        # Build the grid using current candle data
        try:
            current_rate = float(df["close"].iloc[-1])
        except Exception as exc:
            self._logger.error(
                "generate_entry_signal: cannot read current rate for %s (%s); skipping entry",
                pair, exc,
            )
            return ModuleSignal()

        try:
            cfg = self._resolve_config(pair, ctx) or self._build_bootstrap_config(current_rate)
            midprice = GridCalculator.calculate_midprice(df, cfg)
            levels = GridCalculator.generate_levels(cfg, midprice, df=df)
            if len(levels) < 2:
                self._logger.warning(
                    "generate_entry_signal: %s produced %d level(s); skipping entry",
                    pair, len(levels),
                )
                return ModuleSignal()
            self._grid_states[pair] = GridState(
                pair=pair,
                upper_bound=max(levels),
                lower_bound=min(levels),
                grid_levels=levels,
                filled_levels=set(),
                initial_entry_price=current_rate,
                created_at=datetime.now(timezone.utc),
            )
            self._logger.info(
                "Grid built for %s: midprice=%.6f levels=%d range=[%.6f, %.6f]",
                pair, midprice, len(levels), min(levels), max(levels),
            )
        except Exception as exc:
            self._logger.error(
                "generate_entry_signal: failed to build grid for %s: %s", pair, exc
            )
            return ModuleSignal()

        return ModuleSignal(
            enter_long=True,
            entry_tag=f"{self.module_id}:initial_entry",
            confidence=0.8,
        )

    def generate_exit_signal(
        self, df: DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Grid exits are driven by ``adjust_position()`` partial sells.

        This method always returns an empty ``ModuleSignal`` — the module
        never emits an explicit close signal.
        """
        return ModuleSignal()

    # ------------------------------------------------------------------
    # Position adjustment — core grid trading logic
    # ------------------------------------------------------------------
    def adjust_position(  # noqa: PLR0913
        self,
        trade: Any,
        _current_time: Any,
        current_rate: float,
        _current_profit: float,
        min_stake: Optional[float],
        max_stake: float,
        ctx: ModuleContext,
    ) -> Optional[float]:
        """Core grid trading logic called every candle for every open trade.

        The grid is assumed to have been built by ``generate_entry_signal``;
        if no state exists this method is a no-op (defence-in-depth — should
        not happen in normal flow).

        Algorithm
        ---------
        1. Read previous-candle price from ``state.last_price`` (or fall back to
           ``state.initial_entry_price`` on the very first candle after entry).
        2. Compute downward-crossed levels (buy triggers); execute the
           closest unfilled level if stake fits.
        3. Compute upward-crossed *filled* levels (sell triggers); execute a
           partial sell at the lowest such level.
        4. Always update ``state.last_price`` to the current rate so the next
           call has a correct ``prev_rate``.
        """
        pair = getattr(trade, "pair", getattr(ctx, "pair", ""))

        state = self._grid_states.get(pair)
        if state is None:
            self._logger.debug(
                "adjust_position called for %s with no GridState — skipping (entry signal must run first)",
                pair,
            )
            return None

        # Previous-rate proxy: state.last_price after first cycle, otherwise initial_entry_price
        prev_rate: float = (
            state.last_price
            if state.last_price is not None
            else (state.initial_entry_price if state.initial_entry_price is not None else current_rate)
        )

        try:
            # Downward crossing → buy trigger
            crossed_down: List[float] = GridCalculator.levels_crossed_down(
                prev_rate, current_rate, state.grid_levels
            )
            if crossed_down:
                level = crossed_down[0]  # closest crossed level (sorted desc)
                if not state.is_level_filled(level) and state.is_in_range(current_rate):
                    stake_per_level = self._config["initial_stake"] / state.grid_count
                    if min_stake is not None and stake_per_level < min_stake:
                        stake_per_level = min_stake
                    if stake_per_level <= max_stake:
                        self._logger.debug(
                            "Grid BUY at level %.6f for %s (stake=%.4f)",
                            level, pair, stake_per_level,
                        )
                        # Optimistically mark filled so the same level is not
                        # re-triggered while the order is pending.
                        state.mark_filled(level)
                        state.update_last_price(current_rate)
                        return stake_per_level

            # Upward crossing through filled level → sell trigger
            crossed_up: List[float] = GridCalculator.levels_crossed_up(
                prev_rate, current_rate, state.grid_levels, filled=state.filled_levels
            )
            if crossed_up:
                level = crossed_up[0]  # lowest filled level crossed first
                stake_per_level = self._config["initial_stake"] / state.grid_count
                self._logger.debug(
                    "Grid SELL at level %.6f for %s (stake=%.4f)",
                    level, pair, stake_per_level,
                )
                state.update_last_price(current_rate)
                return -stake_per_level

        finally:
            # Always update prev-rate marker, even on no-op candles.
            state.update_last_price(current_rate)

        return None

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------
    def _resolve_config(
        self, pair: str, ctx: ModuleContext
    ) -> Optional[GridConfig]:
        """Return an optimizer-provided ``GridConfig`` for *pair*, or ``None``.

        Resolution order:
          1. In-memory cache (``self._configs[pair]``).
          2. Canonical SharedState key ``(self.module_id, f"{pair}:config")``.
          3. Optuna-style key ``(self.module_id, f"optuna:{symbol}")`` where
             ``symbol`` is ``pair.split("/")[0].split(".")[0]`` (so
             ``"TSLA.US/USD"`` → ``"TSLA"``).

        Returns ``None`` if no optimizer-provided config exists. Callers that
        need a config either way should use ``_build_bootstrap_config()`` as
        the fallback.
        """
        cached = self._configs.get(pair)
        if cached is not None:
            return cached

        # 1) canonical pair-keyed entry
        entry = ctx.shared_state.get(self.module_id, f"{pair}:config")
        if entry and entry.get("data"):
            try:
                cfg = GridConfig.from_dict(entry["data"])
                self._configs[pair] = cfg
                self._logger.info("Loaded GridConfig for %s from SharedState", pair)
                return cfg
            except Exception as exc:
                self._logger.warning(
                    "Failed to load GridConfig for %s: %s", pair, exc
                )

        # 2) Optuna-style entry (bare symbol, no exchange suffix)
        symbol = pair.split("/")[0].split(".")[0]
        entry = ctx.shared_state.get(self.module_id, f"optuna:{symbol}")
        if entry and entry.get("data"):
            try:
                cfg = GridConfig.from_dict(entry["data"])
                self._configs[pair] = cfg
                self._logger.info(
                    "Loaded Optuna GridConfig for %s (symbol=%s)", pair, symbol
                )
                return cfg
            except Exception as exc:
                self._logger.warning(
                    "Failed to load Optuna GridConfig for %s (symbol=%s): %s",
                    pair, symbol, exc,
                )

        return None

    def _build_bootstrap_config(self, current_rate: float) -> GridConfig:
        """Build a default GridConfig from the module's bootstrap config.

        Used by ``generate_entry_signal`` when ``_resolve_config`` returns
        ``None``. Not cached — depends on ``current_rate``.
        """
        upper_pct = self._config["upper_bound_pct"]
        lower_pct = self._config["lower_bound_pct"]
        count = max(2, int(self._config["grid_count"]))
        grid_range = current_rate * (upper_pct + lower_pct)
        grid_distance = grid_range / count
        cfg = GridConfig(grid_distance=grid_distance, grid_range=grid_range)
        self._logger.debug(
            "Bootstrap GridConfig: range=%.6f distance=%.6f (current_rate=%.6f)",
            grid_range, grid_distance, current_rate,
        )
        return cfg

    # ------------------------------------------------------------------
    # Order-fill hook
    # ------------------------------------------------------------------
    def on_order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: Any,
        ctx: ModuleContext,
    ) -> None:
        """Mark/unmark the nearest grid level when a buy/sell order completes."""
        if pair not in self._grid_states:
            return

        state = self._grid_states[pair]

        fill_price: Optional[float] = getattr(order, "price", None)
        if fill_price is None:
            fill_price = getattr(order, "average", None)
        if fill_price is None:
            self._logger.debug(
                "on_order_filled: could not determine fill price for %s — skipping level mark",
                pair,
            )
            return

        nearest = GridCalculator.nearest_level(fill_price, state.grid_levels)
        if nearest is not None:
            order_side: str = getattr(order, "side", "buy")
            if order_side == "sell":
                state.mark_unfilled(nearest)
                self._logger.debug(
                    "Marked level %.6f unfilled for %s after sell fill (fill_price=%.6f)",
                    nearest, pair, fill_price,
                )
            else:
                state.mark_filled(nearest)
                self._logger.debug(
                    "Marked level %.6f filled for %s (fill_price=%.6f)",
                    nearest, pair, fill_price,
                )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def get_module_state(self, pair: str) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of grid state for *pair*."""
        state = self._grid_states.get(pair)
        if state is None:
            return {}
        return state.to_dict()

    def reset_module_state(self, pair: str) -> None:
        """Discard all per-pair internal state for *pair*.

        Pops the ``GridState``, defer counter, and any cached ``GridConfig``.
        ``last_price`` lived on the ``GridState`` so it disappears with it.
        """
        self._grid_states.pop(pair, None)
        self._defer_counts.pop(pair, None)
        self._configs.pop(pair, None)
        self._logger.info("Grid state reset for %s", pair)

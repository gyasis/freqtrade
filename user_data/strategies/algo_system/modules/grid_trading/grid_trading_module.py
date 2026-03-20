"""
modules/grid_trading/grid_trading_module.py
===========================================
GridTradingModule — the first concrete IAlgoModule in the LATS system.

Implements a symmetric grid trading strategy entirely via ``adjust_position()``.
The initial position is opened by ``generate_entry_signal()`` only when no grid
is active for the pair and market conditions are range-bound (low ADX).  After
that, all subsequent buys and partial sells are driven by ``adjust_position()``,
which fires each time the current price crosses a grid level.

Architecture
------------
- ``GridCalculator`` (stateless) handles all pure-math operations.
- ``GridState`` (per-pair dataclass) owns mutable grid bookkeeping.
- This module owns the lifecycle of all ``GridState`` instances and persists
  them to ``SharedState`` on shutdown / restores them on ``on_bot_start``.

Grid trading primer
-------------------
A grid divides a price range [lower, upper] into ``N`` equally-spaced levels.
When the price crosses a level downward, a fixed stake is added (buy).
When the price crosses a previously-bought level upward, a partial sell is
executed to lock in the profit band between adjacent levels.

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
from ...reasoning.entry_quality_evaluator import EntryQualityEvaluator
from ..grid_trading.grid_calculator import GridCalculator
from ..grid_trading.grid_state import GridState

if TYPE_CHECKING:
    pass  # no additional TYPE_CHECKING imports needed


logger = logging.getLogger("algo_system.grid_trading_module")


class GridTradingModule(IAlgoModule):
    """
    Grid trading module for the LATS algorithm orchestration system.

    The module opens an initial position via ``generate_entry_signal()`` then
    manages all subsequent position adjustments (buys at lower grid levels,
    partial sells at upper levels) through ``adjust_position()``.

    Class attributes
    ----------------
    module_id : str
        Unique identifier used as a key in SharedState, log prefixes, and
        entry/exit tag prefixes.
    version : str
        Semantic version.  Increment on breaking state-format changes so that
        restored ``GridState`` dicts from an older version can be migrated or
        discarded gracefully.

    Capability flags
    ----------------
    supports_live is ``PARTIAL`` because freqtrade requires
    ``position_adjustment_enable = True`` in the strategy and the exchange must
    support fractional trades.
    """

    # ------------------------------------------------------------------
    # Class-level identity
    # ------------------------------------------------------------------

    module_id = "grid_trading_v1"
    version = "1.0.0"

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
        self._calculator = GridCalculator()
        self._grid_states: Dict[str, GridState] = {}   # pair -> GridState
        self._config: Dict[str, Any] = {}
        self._defer_counts: Dict[str, int] = {}        # pair -> consecutive defer count
        self._last_prices: Dict[str, float] = {}       # pair -> last seen price (prev_rate proxy)
        self._entry_quality_evaluator: EntryQualityEvaluator = EntryQualityEvaluator({})
        self._logger = logging.getLogger(f"algo_system.{self.module_id}")

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def initialize(self, ctx: ModuleContext) -> None:
        """
        Validate and cache module config from ``SharedState``.

        Config is expected under the key ``(self.module_id, "config")`` in
        the shared state dict.  If not found, sensible defaults are applied.

        Parameters
        ----------
        ctx:
            The ``ModuleContext`` provided by the orchestrator.

        Raises
        ------
        ValueError
            If any required config value is out of bounds.
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
        # Build evaluator config: module defaults to no quality gating (threshold=0.0)
        # unless explicitly set by the user in the algo_system module config.
        evaluator_cfg = {"entry_quality_threshold": 0.0, **raw_cfg}
        self._entry_quality_evaluator = EntryQualityEvaluator(evaluator_cfg)
        self._logger.info(
            "GridTradingModule initialized (config: %s)", self._config
        )

    def on_bot_start(self, ctx: ModuleContext) -> None:
        """
        Restore persisted grid states from ``SharedState``.

        Each pair in the current whitelist is checked for a previously saved
        ``GridState`` dict.  Successfully restored states are loaded back into
        ``self._grid_states``; any that fail to deserialise are skipped with a
        warning so that the bot can continue with a fresh grid for that pair.

        Parameters
        ----------
        ctx:
            The ``ModuleContext`` provided by the orchestrator.
        """
        for pair in ctx.data_provider.current_whitelist():
            entry = ctx.shared_state.get(self.module_id, pair)
            if entry and entry.get("data"):
                try:
                    self._grid_states[pair] = GridState.from_dict(entry["data"])
                    self._logger.info("Restored grid state for %s", pair)
                except Exception as exc:
                    self._logger.warning(
                        "Failed to restore grid state for %s: %s — starting fresh", pair, exc
                    )

    def shutdown(self, ctx: ModuleContext) -> None:
        """
        Persist all active grid states to ``SharedState``.

        This method must not raise — any per-pair serialisation error is caught
        and logged so that other pairs are not affected.

        Parameters
        ----------
        ctx:
            The ``ModuleContext`` provided by the orchestrator.
        """
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
        """
        Attach the ADX indicator column used for entry quality gating.

        The column is named ``_{module_id}_adx`` to avoid collisions with
        columns from other modules.  If ``talib`` is not installed or raises,
        the column is silently omitted and entry quality gating is skipped.

        Parameters
        ----------
        df:
            OHLCV DataFrame for the pair.
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        DataFrame
            The input DataFrame with the ADX column added when available.
        """
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
        """
        Emit an entry signal to open the initial grid position.

        Once a grid is active for this pair, the method returns an empty
        ``ModuleSignal`` (no opinion) because ``adjust_position()`` handles all
        subsequent trades.

        Entry quality gating
        --------------------
        1. ADX > 30 indicates a trending market — grid trading performs poorly
           in trends, so entry is deferred and a consecutive-defer counter is
           incremented.
        2. If deferred for more than 20 consecutive candles a warning is logged
           (but deferral still applies — the market has not become range-bound).
        3. When conditions are acceptable the defer counter is reset and an
           entry signal with ``confidence=0.8`` is returned.

        Parameters
        ----------
        df:
            OHLCV + indicators DataFrame.
        metadata:
            Freqtrade pair metadata dict (must contain ``"pair"`` key).
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        ModuleSignal
            ``enter_long=True`` with ``entry_tag`` prefixed by ``module_id``
            when conditions are met; an empty ``ModuleSignal()`` otherwise.
        """
        pair = metadata.get("pair", getattr(ctx, "pair", ""))

        # Grid already running — position adjustment handles everything
        if pair in self._grid_states and self._grid_states[pair].is_active():
            return ModuleSignal()

        # Entry quality gating via EntryQualityEvaluator
        defer_count = self._defer_counts.get(pair, 0)
        qualifies, rationale = self._entry_quality_evaluator.is_quality_entry(pair, df, defer_count)
        if not qualifies:
            self._defer_counts[pair] = defer_count + 1
            self._logger.debug("Entry deferred for %s: %s", pair, rationale)
            return ModuleSignal()

        self._defer_counts[pair] = 0
        return ModuleSignal(
            enter_long=True,
            entry_tag=f"{self.module_id}:initial_entry",
            confidence=0.8,
        )

    def generate_exit_signal(
        self, df: DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """
        Grid exits are driven by ``adjust_position()`` partial sells.

        This method always returns an empty ``ModuleSignal`` — the module
        never emits an explicit close signal.  The grid is wound down through
        successive partial sells as the price climbs through filled levels.

        Parameters
        ----------
        df:
            OHLCV + indicators DataFrame.
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        ModuleSignal
            Always an empty signal (no opinion on exit).
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
        """
        Core grid trading logic called every candle for every open trade.

        Algorithm
        ---------
        1. If no ``GridState`` exists for this pair, create one centred on
           ``current_rate`` using the configured bounds and level count.
        2. Compute which grid levels were crossed *downward* since the previous
           candle (buy triggers).  If any, execute a buy at the nearest level
           provided the stake fits within ``max_stake`` and the position is
           within the grid range.
        3. Compute which *filled* grid levels were crossed *upward* since the
           previous candle (sell triggers).  If any, execute a partial sell at
           the nearest level.

        Previous-rate proxy
        -------------------
        Freqtrade does not pass the previous candle's close here, so the trade's
        ``open_rate`` is used as a conservative proxy for the starting price of
        the current candle.  This may miss crossings on the very first candle
        after entry; that is acceptable and results in no action rather than a
        false trigger.

        Parameters
        ----------
        trade:
            The freqtrade ``Trade`` object for the open position.
        current_time:
            UTC ``datetime`` of the current candle close.
        current_rate:
            Current market price for the pair.
        current_profit:
            Current profit/loss as a fraction (e.g. ``0.05`` = 5 %).
        min_stake:
            Minimum stake amount accepted by the exchange, or ``None``.
        max_stake:
            Maximum stake amount the bot is allowed to use.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        float or None
            Positive value to add stake (buy); negative to reduce position
            (partial sell); ``None`` for no action.
        """
        pair = getattr(trade, "pair", getattr(ctx, "pair", ""))

        # Initialise grid if not yet present
        if pair not in self._grid_states:
            grid_state = self._initialize_grid(pair, current_rate)
            if grid_state is None:
                return None
            self._grid_states[pair] = grid_state

        state = self._grid_states[pair]

        # Previous-rate proxy: use the last price seen by this module.
        # trade.open_rate is the *initial entry* price and never changes — using
        # it as prev_rate permanently anchors the crossing window at entry,
        # making levels below open_rate permanently invisible after the first candle.
        prev_rate: float = self._last_prices.get(pair, current_rate)

        # ------------------------------------------------------------------
        # Downward crossing → buy trigger
        # ------------------------------------------------------------------
        crossed_down: List[float] = self._calculator.get_crossed_levels_down(
            prev_rate, current_rate, state.grid_levels
        )
        if crossed_down:
            level = crossed_down[0]  # closest level first (sorted desc)
            if not state.is_level_filled(level) and state.is_in_range(current_rate):
                stake_per_level = self._config["initial_stake"] / state.grid_count
                if min_stake is not None and stake_per_level < min_stake:
                    stake_per_level = min_stake
                if stake_per_level <= max_stake:
                    self._logger.debug(
                        "Grid BUY at level %.6f for %s (stake=%.4f)",
                        level,
                        pair,
                        stake_per_level,
                    )
                    # Optimistically mark filled so the same level is not
                    # re-triggered on the next candle while the order is pending.
                    # on_order_filled will confirm the fill; if the order is
                    # rejected the level stays "filled" (conservative — no duplicate buys).
                    state.mark_filled(level)
                    self._last_prices[pair] = current_rate
                    return stake_per_level

        # ------------------------------------------------------------------
        # Upward crossing through filled level → sell trigger
        # ------------------------------------------------------------------
        crossed_up: List[float] = self._calculator.get_crossed_levels_up(
            prev_rate, current_rate, state.grid_levels, state.filled_levels
        )
        if crossed_up:
            level = crossed_up[0]  # lowest filled level crossed first
            stake_per_level = self._config["initial_stake"] / state.grid_count
            self._logger.debug(
                "Grid SELL at level %.6f for %s (stake=%.4f)",
                level,
                pair,
                stake_per_level,
            )
            # Negative stake signals a partial sell to freqtrade
            self._last_prices[pair] = current_rate
            return -stake_per_level

        self._last_prices[pair] = current_rate
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _initialize_grid(self, pair: str, current_rate: float) -> Optional[GridState]:
        """
        Create a new ``GridState`` centred on ``current_rate``.

        Parameters
        ----------
        pair:
            Trading pair string.
        current_rate:
            Current market price used as the centre of the grid.

        Returns
        -------
        GridState or None
            A freshly constructed ``GridState`` on success; ``None`` if the
            ``GridCalculator`` or ``GridState`` raises a ``ValueError`` (e.g.
            degenerate bounds).
        """
        try:
            upper_pct: float = self._config["upper_bound_pct"]
            lower_pct: float = self._config["lower_bound_pct"]
            count: int = self._config["grid_count"]

            upper = current_rate * (1.0 + upper_pct)
            lower = current_rate * (1.0 - lower_pct)
            levels = self._calculator.calculate_levels(lower, upper, count)

            state = GridState(
                pair=pair,
                upper_bound=upper,
                lower_bound=lower,
                grid_count=count,
                grid_levels=levels,
                filled_levels=set(),
                initial_entry_price=current_rate,
                created_at=datetime.now(timezone.utc),
            )
            self._logger.info(
                "Grid initialised for %s: lower=%.6f upper=%.6f count=%d levels=%s",
                pair,
                lower,
                upper,
                count,
                [f"{lv:.6f}" for lv in levels],
            )
            return state
        except ValueError as exc:
            self._logger.error("Failed to initialise grid for %s: %s", pair, exc)
            return None

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
        """
        Mark the nearest grid level as filled when a buy order completes.

        The fill price is read from ``order.price`` (limit) or
        ``order.average`` (market) if available.  If neither attribute exists
        no state change is made and a debug message is logged.

        Parameters
        ----------
        pair:
            Trading pair string.
        trade:
            The freqtrade ``Trade`` object after the fill.
        order:
            The freqtrade ``Order`` object that was filled.
        current_time:
            UTC ``datetime`` at which the fill was processed.
        ctx:
            The shared ``ModuleContext``.
        """
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

        nearest = self._calculator.get_nearest_level(fill_price, state.grid_levels)
        if nearest is not None:
            order_side: str = getattr(order, "side", "buy")
            if order_side == "sell":
                # Sell confirmed — free the level so it can be rebought later
                state.mark_unfilled(nearest)
                self._logger.debug(
                    "Marked level %.6f unfilled for %s after sell fill (fill_price=%.6f)",
                    nearest,
                    pair,
                    fill_price,
                )
            else:
                # Buy confirmed — mark filled (may already be set optimistically)
                state.mark_filled(nearest)
                self._logger.debug(
                    "Marked level %.6f filled for %s (fill_price=%.6f)",
                    nearest,
                    pair,
                    fill_price,
                )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_module_state(self, pair: str) -> Dict[str, Any]:
        """
        Return a JSON-serialisable snapshot of grid state for *pair*.

        Parameters
        ----------
        pair:
            Trading pair string.

        Returns
        -------
        dict
            ``GridState.to_dict()`` output when a grid exists, otherwise
            an empty dict.
        """
        state = self._grid_states.get(pair)
        if state is None:
            return {}
        return state.to_dict()

    def reset_module_state(self, pair: str) -> None:
        """
        Discard all per-pair internal state for *pair*.

        Called by the orchestrator when a pair is removed from the whitelist
        or the module transitions to ``ModuleState.INACTIVE``.

        Parameters
        ----------
        pair:
            Trading pair string.
        """
        self._grid_states.pop(pair, None)
        self._defer_counts.pop(pair, None)
        self._last_prices.pop(pair, None)
        self._logger.info("Grid state reset for %s", pair)

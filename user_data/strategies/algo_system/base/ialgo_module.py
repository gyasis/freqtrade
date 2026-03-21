"""
base/ialgo_module.py
====================
All abstract types and the IAlgoModule ABC that every algorithm module implements.

This module defines the contract every LATS algorithm module must fulfil.  It is
intentionally free of freqtrade internals so that it can be imported cheaply in
tests and in non-trading contexts (e.g. backtesting dry-runs, notebook analysis).

Entities defined here
---------------------
ModuleCapability   – tri-state capability flag (SUPPORTED / UNSUPPORTED / PARTIAL)
ModuleState        – lifecycle FSM states for a module
VALID_TRANSITIONS  – allowed FSM edges
MANAGED_STATES     – shorthand for states that require active order management
ModuleSignal       – the signal dataclass produced per-candle by a module
IAlgoModule        – the ABC that every concrete module must subclass
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Set

from pandas import DataFrame

if TYPE_CHECKING:
    from .module_context import ModuleContext


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ModuleCapability(str, Enum):
    """Tri-state flag describing how well a module supports a given run-mode."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"


class ModuleState(str, Enum):
    """
    Finite-state-machine states for a single algorithm module.

    Lifecycle diagram::

        INACTIVE ──► ACTIVE ──► DRAINING ──► SWITCHING ──► ACTIVE
                       │            │               │
                       ▼            ▼               ▼
                   SUSPENDED    SUSPENDED       SUSPENDED
                       │
                       ▼
                   INACTIVE
    """

    INACTIVE = "inactive"
    ACTIVE = "active"
    DRAINING = "draining"
    SWITCHING = "switching"
    SUSPENDED = "suspended"


# ---------------------------------------------------------------------------
# FSM transition table
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[ModuleState, Set[ModuleState]] = {
    ModuleState.INACTIVE: {ModuleState.ACTIVE},
    ModuleState.ACTIVE: {ModuleState.DRAINING, ModuleState.SUSPENDED},
    ModuleState.DRAINING: {ModuleState.SWITCHING, ModuleState.SUSPENDED},
    ModuleState.SWITCHING: {ModuleState.ACTIVE, ModuleState.SUSPENDED},
    ModuleState.SUSPENDED: {ModuleState.INACTIVE},
}

# States in which open positions must be actively managed (not yet flat).
MANAGED_STATES: tuple[ModuleState, ...] = (
    ModuleState.ACTIVE,
    ModuleState.DRAINING,
    ModuleState.SWITCHING,
)


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------


@dataclass
class ModuleSignal:
    """
    Per-candle signal produced by a single algorithm module.

    Directional flags are three-valued:
      ``True``  – signal fired
      ``False`` – explicit "no signal" / suppress
      ``None``  – module has no opinion (neutral; the orchestrator will ignore it)

    Tag convention
    --------------
    Both ``entry_tag`` and ``exit_tag`` **must** be prefixed with ``"{module_id}:"``
    so that the orchestrator can trace which module produced the tag.  Example::

        entry_tag = "momentum_v1:bullish_breakout"

    Attributes
    ----------
    enter_long:
        Long entry signal.
    enter_short:
        Short entry signal.
    exit_long:
        Signal to close a long position.
    exit_short:
        Signal to close a short position.
    entry_tag:
        Human-readable tag for the entry reason.  Must be prefixed
        ``"{module_id}:"``.
    exit_tag:
        Human-readable tag for the exit reason.
    confidence:
        Normalised confidence score in ``[0.0, 1.0]`` (default ``1.0``).
        Used by the orchestrator when weighting conflicting module signals.
    metadata:
        Arbitrary module-specific data forwarded to the orchestrator for
        logging and downstream use (e.g. raw indicator values, sub-scores).
    """

    enter_long: Optional[bool] = None
    enter_short: Optional[bool] = None
    exit_long: Optional[bool] = None
    exit_short: Optional[bool] = None
    entry_tag: Optional[str] = None  # MUST be prefixed "{module_id}:"
    exit_tag: Optional[str] = None
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    def has_entry_signal(self) -> bool:
        """Return ``True`` if at least one entry direction is explicitly set."""
        return self.enter_long is True or self.enter_short is True

    def has_exit_signal(self) -> bool:
        """Return ``True`` if at least one exit direction is explicitly set."""
        return self.exit_long is True or self.exit_short is True


# ---------------------------------------------------------------------------
# IAlgoModule ABC
# ---------------------------------------------------------------------------


class IAlgoModule(ABC):
    """
    Abstract base class that every LATS algorithm module must subclass.

    Class-level variables
    ---------------------
    Subclasses **must** override ``module_id`` and ``version``.  The capability
    flags have sensible defaults but should be overridden to reflect the actual
    capability of the concrete implementation.

    Lifecycle
    ---------
    The orchestrator calls::

        initialize()    – once at startup, before any candles are processed
        on_bot_start()  – once the bot has connected to the exchange / data feed
        shutdown()      – when the bot is stopped or the module is unloaded

    Per-candle pipeline
    -------------------
    For each pair on each candle the orchestrator calls, in order::

        populate_indicators()    – attach raw indicator columns to *df*
        generate_entry_signal()  – produce a ModuleSignal for entry logic
        generate_exit_signal()   – produce a ModuleSignal for exit logic

    Trade-management hooks (optional)
    -----------------------------------
    ``adjust_position``, ``custom_stoploss``, and ``on_order_filled`` all have
    default no-op implementations.  Override only what the module needs.

    Introspection
    -------------
    ``get_module_state`` and ``reset_module_state`` must be implemented so that
    the orchestrator and observability layer can inspect and recover per-pair
    state without reaching into module internals.
    """

    # ------------------------------------------------------------------
    # Class-level identity / capability flags
    # Subclasses MUST override module_id and version.
    # ------------------------------------------------------------------

    module_id: ClassVar[str]
    version: ClassVar[str]

    supports_backtest: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_paper: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_live: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_hyperopt: ClassVar[ModuleCapability] = ModuleCapability.PARTIAL
    supports_short: ClassVar[ModuleCapability] = ModuleCapability.UNSUPPORTED
    supports_position_adjust: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED

    # ------------------------------------------------------------------
    # Lifecycle hooks — abstract; every module MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def initialize(self, context: "ModuleContext") -> None:
        """
        Called once during bot startup, before any candle data arrives.

        Use this hook to allocate per-module resources (e.g. indicator
        buffers, ML model loading, database connections).

        Parameters
        ----------
        context:
            The shared :class:`~base.module_context.ModuleContext` provided
            by the orchestrator.
        """

    @abstractmethod
    def on_bot_start(self, context: "ModuleContext") -> None:
        """
        Called once after the bot has successfully connected to the exchange
        and the initial market data snapshot is available.

        Use this hook for any initialisation that requires live exchange
        data (e.g. fetching the current order book, validating pair lists).

        Parameters
        ----------
        context:
            The shared :class:`~base.module_context.ModuleContext`.
        """

    @abstractmethod
    def shutdown(self, context: "ModuleContext") -> None:
        """
        Called when the bot is shutting down or this module is being unloaded.

        Release all resources allocated in :meth:`initialize` and
        :meth:`on_bot_start`.  The implementation must be idempotent — calling
        it multiple times must not raise.

        Parameters
        ----------
        context:
            The shared :class:`~base.module_context.ModuleContext`.
        """

    # ------------------------------------------------------------------
    # Per-candle signal production — abstract
    # ------------------------------------------------------------------

    @abstractmethod
    def populate_indicators(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: "ModuleContext",
    ) -> DataFrame:
        """
        Compute and attach indicator columns to *df*.

        This method mirrors freqtrade's ``IStrategy.populate_indicators``
        contract.  The returned DataFrame **must** contain at least all columns
        that were present on entry; it **must not** drop existing columns.

        Naming convention: columns added by this module should be prefixed
        ``"{module_id}_"`` to avoid collisions (e.g. ``"momentum_v1_rsi"``).

        Parameters
        ----------
        df:
            OHLCV DataFrame for the pair, may already contain columns from
            other modules that ran earlier in the pipeline.
        metadata:
            Freqtrade pair metadata dict (keys: ``"pair"``, etc.).
        ctx:
            The shared :class:`~base.module_context.ModuleContext`.

        Returns
        -------
        DataFrame
            The same DataFrame with additional indicator columns attached.
        """

    @abstractmethod
    def generate_entry_signal(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: "ModuleContext",
    ) -> ModuleSignal:
        """
        Inspect the final row of *df* and produce an entry signal.

        Implementations should only read the last completed candle
        (``df.iloc[-1]``) unless they have a specific multi-candle pattern
        requirement.

        Parameters
        ----------
        df:
            OHLCV + indicators DataFrame (output of
            :meth:`populate_indicators`).
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared :class:`~base.module_context.ModuleContext`.

        Returns
        -------
        ModuleSignal
            A signal where unset flags (``None``) mean "no opinion".
            ``entry_tag`` must be prefixed ``"{module_id}:"``.
        """

    @abstractmethod
    def generate_exit_signal(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: "ModuleContext",
    ) -> ModuleSignal:
        """
        Inspect the final row of *df* and produce an exit signal.

        Parameters
        ----------
        df:
            OHLCV + indicators DataFrame.
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared :class:`~base.module_context.ModuleContext`.

        Returns
        -------
        ModuleSignal
            A signal where unset flags (``None``) mean "no opinion".
            ``exit_tag`` must be prefixed ``"{module_id}:"``.
        """

    # ------------------------------------------------------------------
    # Trade management hooks — optional, concrete default implementations
    # ------------------------------------------------------------------

    def adjust_position(  # noqa: PLR0913
        self,
        _trade: Any,
        _current_time: Any,
        _current_rate: float,
        _current_profit: float,
        _min_stake: Optional[float],
        _max_stake: float,
        _ctx: "ModuleContext",
    ) -> Optional[float]:
        """
        Request a position size adjustment for an open trade.

        The orchestrator calls this on every candle for every open trade
        **only** when the module is in a :data:`MANAGED_STATES` state.

        Parameters
        ----------
        trade:
            The freqtrade ``Trade`` object (typed as ``Any`` to avoid
            importing freqtrade internals into this base file).
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
            The shared :class:`~base.module_context.ModuleContext`.

        Returns
        -------
        float or None
            Positive value to increase position, negative to reduce, ``None``
            to leave the position unchanged.  Default implementation always
            returns ``None``.
        """
        return None

    def custom_stoploss(
        self,
        _pair: str,
        _trade: Any,
        _current_time: Any,
        _current_rate: float,
        _current_profit: float,
        _ctx: "ModuleContext",
    ) -> Optional[float]:
        """
        Return a custom stoploss value for the given trade.

        Parameters
        ----------
        pair:
            Trading pair string (e.g. ``"BTC/USDT"``).
        trade:
            The freqtrade ``Trade`` object.
        current_time:
            UTC ``datetime`` of the current candle.
        current_rate:
            Current market price.
        current_profit:
            Current profit/loss as a fraction.
        ctx:
            The shared :class:`~base.module_context.ModuleContext`.

        Returns
        -------
        float or None
            Stoploss distance as a *negative* fraction
            (e.g. ``-0.02`` = 2 % below current rate), or ``None`` to
            delegate stoploss management to the orchestrator/strategy.
            Default implementation always returns ``None``.
        """
        return None

    def on_order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: Any,
        ctx: "ModuleContext",
    ) -> None:
        """
        Notification hook called after an order is confirmed filled.

        Use this to update module-internal state (e.g. reset trailing
        buffers, record entry price for a custom stoploss calculation).

        Parameters
        ----------
        pair:
            Trading pair string.
        trade:
            The freqtrade ``Trade`` object after the fill is applied.
        order:
            The freqtrade ``Order`` object that was filled (typed as ``Any``
            to avoid importing freqtrade internals).
        current_time:
            UTC ``datetime`` at which the fill was processed.
        ctx:
            The shared :class:`~base.module_context.ModuleContext`.
        """

    # ------------------------------------------------------------------
    # Introspection — abstract
    # ------------------------------------------------------------------

    @abstractmethod
    def get_module_state(self, pair: str) -> Dict[str, Any]:
        """
        Return a serialisable snapshot of this module's internal state for
        the given *pair*.

        The returned dict must be JSON-serialisable (no DataFrames, no
        complex objects) so that it can be written to the observability
        layer without further processing.

        Parameters
        ----------
        pair:
            Trading pair string (e.g. ``"BTC/USDT"``).

        Returns
        -------
        dict
            Snapshot of module state.  At minimum the dict should contain
            ``{"module_id": ..., "pair": pair}``.
        """

    @abstractmethod
    def reset_module_state(self, pair: str) -> None:
        """
        Reset all per-pair internal state for *pair* to its initial values.

        Called by the orchestrator when a pair is removed from the trade list
        or when the module transitions to :attr:`ModuleState.INACTIVE`.

        Parameters
        ----------
        pair:
            Trading pair string.
        """

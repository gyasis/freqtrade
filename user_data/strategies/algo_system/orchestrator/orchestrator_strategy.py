"""
orchestrator/orchestrator_strategy.py
OrchestratorStrategy — the single IStrategy loaded by freqtrade.
Delegates ALL trade decisions to registered algorithm modules.

Phase 1 skeleton: covers backtest and paper trading.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Union

from freqtrade.strategy import IStrategy
from pandas import DataFrame

from ..base.ialgo_module import ModuleState
from ..base.module_context import DataProviderProxy, ModuleContext, WalletsProxy
from ..config.algo_config_schema import validate_algo_config
from ..observability.metrics_collector import MetricsCollector
from ..observability.module_logger import get_system_logger
from ..safety.circuit_breaker import CircuitBreaker
from .algo_lifecycle import AlgoLifecycleSM
from .budget_allocator import BudgetAllocator
from .module_registry import ModuleRegistry
from .shared_state import SharedState
from .signal_arbiter import SignalArbiter

logger = get_system_logger("orchestrator")


class OrchestratorStrategy(IStrategy):
    """
    Single-entry-point strategy that orchestrates N algorithm modules.

    Subsystems owned:
    - ModuleRegistry: discovers and holds IAlgoModule instances
    - SignalArbiter: resolves conflicting module signals
    - BudgetAllocator: enforces per-module capital quotas
    - AlgoLifecycleSM: per-pair module state machine
    - CircuitBreaker: independent safety halt
    - SharedState: cross-module persistent key-value store
    - IReasoningEngine: selects authoritative module per-pair per-candle
    """

    # ---- Freqtrade required class-level config ----
    INTERFACE_VERSION = 3
    position_adjustment_enable = True
    max_entry_position_adjustment = 10
    stoploss = -0.10
    timeframe = "1h"
    can_short = False
    use_custom_stoploss = False

    # ---- Subsystems (initialized in __init__) ----
    _registry: ModuleRegistry
    _arbiter: SignalArbiter
    _budget: BudgetAllocator
    _lifecycle: AlgoLifecycleSM
    _circuit_breaker: CircuitBreaker
    _shared_state: SharedState
    _reasoning_engine: Any  # IReasoningEngine
    _algo_config: dict
    _failure_counts: Dict[str, Dict[str, int]]  # module_id -> pair -> count
    _metrics: Any  # MetricsCollector — initialized in bot_start
    _routing_decisions: Dict[str, Any]  # pair -> current RoutingDecision
    _MAX_FAILURES = 3

    def __init__(self, config: dict) -> None:
        super().__init__(config)

        raw_algo_cfg = config.get("algo_system", {})
        self._algo_config = validate_algo_config(raw_algo_cfg)

        # Boot subsystems
        self._lifecycle = AlgoLifecycleSM()
        self._shared_state = SharedState(
            persistence_path=self._algo_config["shared_state"]["persistence_path"]
        )
        self._budget = BudgetAllocator()
        self._arbiter = SignalArbiter(priority_order=self._algo_config["active_modules"])
        self._circuit_breaker = CircuitBreaker(self._algo_config["circuit_breaker"])
        self._registry = ModuleRegistry(self._lifecycle)
        self._failure_counts = {}
        self._routing_decisions: Dict[str, Any] = {}  # pair -> RoutingDecision
        self._cb_alerted: bool = False  # track CB alert-sent state

        # Boot reasoning engine
        reasoning_cfg = self._algo_config["reasoning"]
        engine_type = reasoning_cfg.get("engine", "rule_based")
        if engine_type == "rule_based":
            from ..reasoning.rule_based_reasoning import RuleBasedReasoningEngine

            self._reasoning_engine = RuleBasedReasoningEngine()
        else:
            from ..reasoning.rule_based_reasoning import RuleBasedReasoningEngine

            logger.warning(
                "Unknown reasoning engine '%s', falling back to rule_based", engine_type
            )
            self._reasoning_engine = RuleBasedReasoningEngine()

        logger.info(
            "OrchestratorStrategy initialized: modules=%s engine=%s",
            self._algo_config["active_modules"],
            engine_type,
        )

    def bot_start(self, **kwargs: Any) -> None:
        """Called after DataProvider is ready. Discover+register modules."""
        ctx = self._make_context("__init__", datetime.now(timezone.utc))

        # Load persisted state
        self._shared_state.load_from_disk()

        # Discover modules
        self._registry.discover_and_register(
            modules_path=self._algo_config["modules_path"],
            active_module_ids=self._algo_config["active_modules"],
            context=ctx,
        )

        # Initialize reasoning engine
        self._reasoning_engine.initialize(
            self._algo_config["reasoning"],
            self._registry.get_all_ids(),
        )

        # Register budget allocations
        for mid in self._registry.get_all_ids():
            module_cfg = self._algo_config["modules"].get(mid, {})
            self._budget.register_module(
                module_id=mid,
                max_open_trades=module_cfg.get(
                    "max_open_trades", self.config.get("max_open_trades", 5)
                ),
                stake_allocation_pct=module_cfg.get("stake_allocation_pct", 1.0),
            )

        # Notify modules bot has started
        for mod in self._registry.get_all():
            self._safe_call(
                mod.on_bot_start, ctx, module_id=mod.module_id, pair="__all__"
            )

        logger.info("OrchestratorStrategy bot_start complete")

        # Wire MetricsCollector once DataProvider is ready
        self._metrics = MetricsCollector(self.dp)

    # ------------------------------------------------------------------ #
    # Core IStrategy hooks                                                 #
    # ------------------------------------------------------------------ #

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        ctx = self._make_context(pair, datetime.now(timezone.utc))

        # Circuit breaker check (no orders from populate, but log status)
        if self._circuit_breaker.is_tripped():
            return dataframe

        # Fan out indicator population to all modules
        for mod in self._registry.get_all():
            result = self._safe_call(
                mod.populate_indicators,
                dataframe,
                metadata,
                ctx,
                default_retval=dataframe,
                module_id=mod.module_id,
                pair=pair,
            )
            if isinstance(result, DataFrame):
                dataframe = result

        # Compute routing decision once per candle (after all indicators are populated)
        active_ids = self._registry.get_all_ids()
        prev = self._routing_decisions.get(pair)
        routing = self._reasoning_engine.get_routing_decision(pair, dataframe, active_ids, prev)
        self._routing_decisions[pair] = routing

        # Detect routing change and emit metric
        if (prev is None) != (routing is None) or (
            prev and routing and prev.authoritative_module_id != routing.authoritative_module_id
        ):
            from_mod = prev.authoritative_module_id if prev else "none"
            to_mod = routing.authoritative_module_id if routing else "none"
            rationale = routing.rationale if routing else "no qualifying module"
            if hasattr(self, "_metrics"):
                self._metrics.send_routing_change(from_mod, to_mod, pair, rationale)

        # Tick the reasoning engine if it supports it
        if hasattr(self._reasoning_engine, "tick"):
            self._reasoning_engine.tick()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""

        # Circuit breaker — alert once on trip, clear flag on recovery
        if self._circuit_breaker.is_tripped():
            if not self._cb_alerted and hasattr(self, "_metrics"):
                reason = self._circuit_breaker.state.trip_reason or "threshold exceeded"
                self._metrics.send_circuit_breaker_alert(reason)
                self._cb_alerted = True
            return dataframe
        self._cb_alerted = False

        ctx = self._make_context(pair, datetime.now(timezone.utc))

        # Read pre-computed routing decision (set in populate_indicators)
        routing = self._routing_decisions.get(pair)

        # Collect signals from all modules
        signals = {}
        for mod in self._registry.get_all():
            sig = self._safe_call(
                mod.generate_entry_signal,
                dataframe,
                metadata,
                ctx,
                default_retval=None,
                module_id=mod.module_id,
                pair=pair,
            )
            if sig is not None:
                signals[mod.module_id] = sig

        winner_id, winning_signal = self._arbiter.arbitrate(signals, routing)

        if winner_id and winning_signal and winning_signal.has_entry_signal():
            # Budget check
            max_ot = self.config.get("max_open_trades", 5)
            stake = self.wallets.get_trade_stake_amount(pair, max_ot) if self.wallets else 100.0
            total = self.wallets.get_total_stake_amount() if self.wallets else 10000.0
            allowed, reason = self._budget.can_open_trade(winner_id, stake, total)
            if allowed:
                dataframe.loc[dataframe.index[-1], "enter_long"] = 1
                # Validate enter_tag format: must be "{module_id}:{reason}", max 255 chars
                tag = winning_signal.entry_tag or f"{winner_id}:entry"
                if ":" not in tag or len(tag) > 255:
                    logger.warning(
                        "Invalid enter_tag format '%s' from %s — expected "
                        "'{module_id}:{reason}', max 255 chars",
                        tag,
                        winner_id,
                    )
                    tag = f"{winner_id}:entry"  # safe fallback
                dataframe.loc[dataframe.index[-1], "enter_tag"] = tag
                # Transition module state to ACTIVE for this pair
                self._lifecycle.transition(winner_id, pair, ModuleState.ACTIVE)
            else:
                logger.debug("Entry blocked for %s/%s: %s", winner_id, pair, reason)

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        dataframe["exit_long"] = 0

        if self._circuit_breaker.is_tripped():
            return dataframe

        ctx = self._make_context(pair, datetime.now(timezone.utc))

        # Read pre-computed routing decision (set in populate_indicators)
        routing = self._routing_decisions.get(pair)

        signals = {}
        for mod in self._registry.get_all():
            sig = self._safe_call(
                mod.generate_exit_signal,
                dataframe,
                metadata,
                ctx,
                default_retval=None,
                module_id=mod.module_id,
                pair=pair,
            )
            if sig is not None:
                signals[mod.module_id] = sig

        winner_id, winning_signal = self._arbiter.arbitrate(signals, routing)
        if winner_id and winning_signal and winning_signal.has_exit_signal():
            dataframe.loc[dataframe.index[-1], "exit_long"] = 1

        return dataframe

    def adjust_trade_position(
        self,
        trade: Any,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: Optional[float],
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs: Any,
    ) -> Optional[Union[float, Tuple[float, str]]]:
        """Route position adjustments to the module that owns this trade."""
        pair = trade.pair

        # Circuit breaker — no new positions
        if self._circuit_breaker.is_tripped():
            return None

        ctx = self._make_context(pair, current_time)

        # Find the module that should manage this trade (from enter_tag prefix)
        owner_id = self._get_trade_owner(trade)
        if owner_id is None:
            return None

        mod = self._registry.get(owner_id)
        if mod is None:
            return None

        result = self._safe_call(
            mod.adjust_position,
            trade,
            current_time,
            current_rate,
            current_profit,
            min_stake,
            max_stake,
            ctx,
            default_retval=None,
            module_id=owner_id,
            pair=pair,
        )
        return result

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs: Any,
    ) -> bool:
        """Final budget gate before order is placed."""
        owner_id = (entry_tag.split(":")[0] if entry_tag and ":" in entry_tag else None)
        if owner_id is None:
            return True

        stake = amount * rate
        total = self.wallets.get_total_stake_amount() if self.wallets else 10000.0
        allowed, reason = self._budget.can_open_trade(owner_id, stake, total)
        if not allowed:
            logger.warning(
                "confirm_trade_entry blocked for %s/%s: %s", owner_id, pair, reason
            )
        # record_open is called in order_filled once the exchange confirms the fill —
        # calling it here too would double-count every trade's stake/slot usage.
        return allowed

    def order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: datetime,
        **kwargs: Any,
    ) -> None:
        """Route order fill notification to the owning module."""
        owner_id = self._get_trade_owner(trade)
        if owner_id is None:
            return

        mod = self._registry.get(owner_id)
        if mod is None:
            return

        ctx = self._make_context(pair, current_time)
        self._safe_call(
            mod.on_order_filled,
            pair,
            trade,
            order,
            current_time,
            ctx,
            default_retval=None,
            module_id=owner_id,
            pair=pair,
        )

        # Record trade open in budget after confirmed fill
        if hasattr(order, "side") and getattr(order, "side", "") == "buy":
            stake = getattr(order, "cost", 0.0) or (
                getattr(order, "filled", 0.0) * getattr(order, "price", 0.0)
            )
            if stake > 0:
                self._budget.record_open(owner_id, stake)

        if hasattr(self, "_metrics"):
            self._metrics.send_info(f"order_filled: {owner_id}/{pair}")

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _make_context(self, pair: str, current_time: datetime) -> ModuleContext:
        import logging as _log

        dp_proxy = DataProviderProxy(self.dp) if self.dp else DataProviderProxy(None)
        wallets_proxy = WalletsProxy(self.wallets) if self.wallets else WalletsProxy(None)
        return ModuleContext(
            pair=pair,
            run_mode=self.config.get("runmode"),
            current_time=current_time,
            data_provider=dp_proxy,
            wallets=wallets_proxy,
            shared_state=self._shared_state,
            reasoning_hints=self._routing_decisions.get(pair),
            logger=_log.getLogger(f"algo_system.__context__.{pair}"),
            module_id="orchestrator",
        )

    def _safe_call(
        self,
        fn,
        *args,
        default_retval=None,
        module_id: str = "",
        pair: str = "",
        **kwargs,
    ):
        """
        Exception-isolated module call. Mirrors freqtrade's strategy_safe_wrapper.
        On exception: records failure, auto-suspends after _MAX_FAILURES consecutive failures.
        """
        try:
            result = fn(*args)
            self._reset_failure(module_id, pair)
            return result
        except Exception as exc:
            count = self._record_failure(module_id, pair)
            logger.error(
                "Module '%s' error on %s (%d/%d): %s",
                module_id,
                pair,
                count,
                self._MAX_FAILURES,
                exc,
            )
            if count >= self._MAX_FAILURES:
                self._suspend_module(module_id, pair)
            return default_retval

    def _record_failure(self, module_id: str, pair: str) -> int:
        return self._lifecycle.record_failure(module_id, pair)

    def _reset_failure(self, module_id: str, pair: str) -> None:
        self._lifecycle.reset_failure_count(module_id, pair)

    def _suspend_module(self, module_id: str, pair: str) -> None:
        self._lifecycle.transition(module_id, pair, ModuleState.SUSPENDED)
        self._budget.record_suspension(module_id)
        logger.error(
            "Module '%s' SUSPENDED for %s after %d consecutive failures",
            module_id,
            pair,
            self._MAX_FAILURES,
        )
        if hasattr(self, "_metrics"):
            self._metrics.send_suspension_alert(module_id, pair, self._MAX_FAILURES)

    def _get_trade_owner(self, trade: Any) -> Optional[str]:
        """Extract module_id from trade.enter_tag prefix (format: 'module_id:reason')."""
        tag = getattr(trade, "enter_tag", "") or ""
        if ":" in tag:
            return tag.split(":")[0]
        return None

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        """Called every bot loop iteration. Evaluate circuit breaker."""
        # Minimal CB check using portfolio state.
        # Full drawdown calculation requires Trade.get_overall_performance() — done in future phase.
        pass

    def __del__(self) -> None:
        try:
            self._shared_state.save_to_disk()
        except Exception:
            pass

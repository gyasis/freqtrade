"""
orchestrator/module_registry.py
Discovers, instantiates, and manages IAlgoModule instances.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Type

from pandas import DataFrame

from ..base.ialgo_module import IAlgoModule, ModuleCapability, ModuleState
from ..base.module_context import ModuleContext
from .algo_lifecycle import AlgoLifecycleSM
from .module_resolver import ModuleResolver
from .pair_selector import PairSelector

logger = logging.getLogger("algo_system.registry")


class ModuleRegistry:
    """
    Owns the set of active IAlgoModule instances.
    Handles discovery (via ModuleResolver), initialization, and capability gating.
    """

    def __init__(self, lifecycle: AlgoLifecycleSM) -> None:
        self._modules: Dict[str, IAlgoModule] = {}  # module_id -> instance
        self._lifecycle = lifecycle
        self._lock = threading.Lock()
        self._resolver = ModuleResolver()
        self._pair_selector: Optional[PairSelector] = None

    def discover_and_register(
        self,
        modules_path: str,
        active_module_ids: List[str],
        context: ModuleContext,
    ) -> None:
        """
        Scan modules_path for IAlgoModule subclasses, filter to active_module_ids,
        check RunMode capability, instantiate and initialize each.

        Raises ValueError if a required module_id is not found after scanning.
        Logs and skips modules whose capability is UNSUPPORTED for current RunMode.
        """
        from freqtrade.enums import RunMode

        run_mode = context.run_mode

        # Map RunMode -> capability attribute
        _MODE_CAP = {
            RunMode.LIVE: "supports_live",
            RunMode.DRY_RUN: "supports_paper",
            RunMode.BACKTEST: "supports_backtest",
            RunMode.HYPEROPT: "supports_hyperopt",
        }
        cap_attr = _MODE_CAP.get(run_mode, "supports_paper")

        discovered = self._resolver.discover_modules(modules_path)
        discovered_by_id: Dict[str, Type[IAlgoModule]] = {
            cls.module_id: cls for cls in discovered
        }

        for mid in active_module_ids:
            cls = discovered_by_id.get(mid)
            if cls is None:
                raise ValueError(
                    f"ModuleRegistry: module_id '{mid}' not found in {modules_path}. "
                    f"Available: {list(discovered_by_id.keys())}"
                )
            cap = getattr(cls, cap_attr, ModuleCapability.UNSUPPORTED)
            if cap == ModuleCapability.UNSUPPORTED:
                logger.warning(
                    "Module '%s' does not support run_mode=%s (%s=UNSUPPORTED) — skipped",
                    mid,
                    run_mode,
                    cap_attr,
                )
                continue
            self.register(cls(), context)

    def register(self, module: IAlgoModule, context: ModuleContext) -> None:
        """Register a pre-instantiated module. Calls initialize()."""
        with self._lock:
            mid = module.module_id
            if mid in self._modules:
                logger.warning("Module '%s' already registered — skipping duplicate", mid)
                return
            try:
                module.initialize(context)
                self._modules[mid] = module
                self._lifecycle.register_module(mid)
                logger.info("Module '%s' v%s registered", mid, module.version)
            except Exception as exc:
                logger.error("Failed to initialize module '%s': %s", mid, exc)
                raise

    def get(self, module_id: str) -> Optional[IAlgoModule]:
        """Return module instance by ID, or None if not registered."""
        return self._modules.get(module_id)

    def get_all(self) -> List[IAlgoModule]:
        """Return all registered module instances."""
        with self._lock:
            return list(self._modules.values())

    def get_all_ids(self) -> List[str]:
        with self._lock:
            return list(self._modules.keys())

    def get_active_for_pair(self, module_id: str, pair: str) -> Optional[IAlgoModule]:
        """Return module only if it is in a MANAGED state for this pair."""
        mod = self._modules.get(module_id)
        if mod and self._lifecycle.is_managed(module_id, pair):
            return mod
        return None

    def configure_pair_selector(self, config: dict) -> None:
        """Configure and enable the PairSelector from a config dict."""
        if config.get("enabled", True):
            self._pair_selector = PairSelector(config)
            logger.info(
                "PairSelector configured: eval_interval=%d",
                config.get("evaluation_interval_candles", 24),
            )

    def update_active_pairs(
        self,
        module_id: str,
        whitelist: List[str],
        df_map: Dict[str, DataFrame],
    ) -> None:
        """
        Re-evaluate which pairs are active for module_id.
        Transitions pairs removed by PairSelector to DRAINING state.
        """
        if self._pair_selector is None:
            return
        if not self._pair_selector.should_evaluate():
            self._pair_selector.tick()
            return
        added, removed = self._pair_selector.update_active_pairs(module_id, whitelist, df_map)
        for pair in removed:
            if self._lifecycle.get_state(module_id, pair) in (ModuleState.ACTIVE,):
                try:
                    self._lifecycle.transition(module_id, pair, ModuleState.DRAINING)
                    logger.info(
                        "PairSelector: pair %s transitioning to DRAINING for %s", pair, module_id
                    )
                except Exception as exc:
                    logger.warning(
                        "PairSelector: could not transition %s/%s to DRAINING: %s",
                        module_id, pair, exc,
                    )
        self._pair_selector.tick()

    def get_active_pairs_for_module(self, module_id: str) -> List[str]:
        """Return pairs active for module_id per PairSelector; all module IDs if no selector."""
        if self._pair_selector is not None:
            return self._pair_selector.get_active_pairs(module_id)
        return list(self._modules.keys())

    def shutdown_all(self, context: ModuleContext) -> None:
        """Call shutdown() on all modules. Does not raise."""
        for mid, mod in list(self._modules.items()):
            try:
                mod.shutdown(context)
                logger.info("Module '%s' shut down", mid)
            except Exception as exc:
                logger.error("Error shutting down module '%s': %s", mid, exc)

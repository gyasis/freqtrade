"""
tests/test_orchestrator.py
Budget enforcement unit tests for BudgetAllocator (T031).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_STRAT_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tempfile

from algo_system.base.ialgo_module import ModuleState
from algo_system.orchestrator.algo_lifecycle import AlgoLifecycleSM
from algo_system.orchestrator.budget_allocator import BudgetAllocator
from algo_system.orchestrator.module_registry import ModuleRegistry
from algo_system.orchestrator.module_resolver import ModuleResolver
from test_helpers import make_ohlcv_df


def _make_allocator(
    module_id: str = "grid_trading_v1",
    max_open_trades: int = 5,
    stake_pct: float = 0.5,
) -> BudgetAllocator:
    alloc = BudgetAllocator()
    alloc.register_module(module_id, max_open_trades=max_open_trades, stake_allocation_pct=stake_pct)
    return alloc


# ===========================================================================
# Registration
# ===========================================================================

class TestBudgetAllocatorRegistration:
    def test_register_and_retrieve(self) -> None:
        alloc = _make_allocator()
        util = alloc.get_utilization("grid_trading_v1")
        assert util["module_id"] == "grid_trading_v1"
        assert util["max_open_trades"] == 5

    def test_duplicate_registration_raises(self) -> None:
        alloc = _make_allocator()
        with pytest.raises(ValueError, match="already registered"):
            alloc.register_module("grid_trading_v1", max_open_trades=3, stake_allocation_pct=0.3)

    def test_unregistered_module_returns_not_registered(self) -> None:
        alloc = BudgetAllocator()
        allowed, reason = alloc.can_open_trade("unknown_mod", 100.0, 10_000.0)
        assert allowed is False
        assert "not_registered" in reason


# ===========================================================================
# Trade quota cap
# ===========================================================================

class TestBudgetTradeQuota:
    def test_first_trade_allowed(self) -> None:
        alloc = _make_allocator(max_open_trades=5, stake_pct=1.0)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is True
        assert reason == ""

    def test_quota_cap_blocks_extra_trade(self) -> None:
        alloc = _make_allocator(max_open_trades=3, stake_pct=1.0)
        for _ in range(3):
            alloc.record_open("grid_trading_v1", 100.0)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is False
        assert "trade_quota_exceeded" in reason

    def test_quota_freed_after_close(self) -> None:
        alloc = _make_allocator(max_open_trades=1, stake_pct=1.0)
        alloc.record_open("grid_trading_v1", 100.0)
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is False

        alloc.record_close("grid_trading_v1", 100.0)
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is True

    def test_11th_trade_blocked_at_limit_10(self) -> None:
        alloc = _make_allocator(max_open_trades=10, stake_pct=1.0)
        for _ in range(10):
            alloc.record_open("grid_trading_v1", 10.0)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 10.0, 10_000.0)
        assert allowed is False
        assert "trade_quota_exceeded" in reason


# ===========================================================================
# Stake cap
# ===========================================================================

class TestBudgetStakeCap:
    def test_stake_within_cap_allowed(self) -> None:
        # 50% of 10_000 = 5000 cap; propose 100 → OK
        alloc = _make_allocator(stake_pct=0.5)
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is True

    def test_stake_exceeds_cap_blocked(self) -> None:
        # 50% of 1000 = 500 cap; propose 600 → blocked
        alloc = _make_allocator(stake_pct=0.5)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 600.0, 1_000.0)
        assert allowed is False
        assert "stake_cap_exceeded" in reason

    def test_cumulative_stake_blocks_when_full(self) -> None:
        alloc = _make_allocator(max_open_trades=10, stake_pct=0.5)
        total = 10_000.0
        # cap = total * 0.5 = 5000
        # fill 4900 of 5000
        alloc.record_open("grid_trading_v1", 4_900.0)
        # propose 200 → 4900+200=5100 > 5000 → blocked
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 200.0, total)
        assert allowed is False
        assert "stake_cap_exceeded" in reason

    def test_stake_released_after_close(self) -> None:
        alloc = _make_allocator(max_open_trades=10, stake_pct=0.5)
        alloc.record_open("grid_trading_v1", 4_900.0)
        alloc.record_close("grid_trading_v1", 4_900.0)
        # Now room is restored
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 200.0, 10_000.0)
        assert allowed is True


# ===========================================================================
# Suspension
# ===========================================================================

class TestBudgetSuspension:
    def test_suspended_module_quota_stays_reserved(self) -> None:
        alloc = _make_allocator(max_open_trades=5, stake_pct=0.5)
        alloc.record_open("grid_trading_v1", 500.0)
        alloc.record_suspension("grid_trading_v1")

        allowed, reason = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is False
        assert "suspended" in reason

        # Stake counter is still intact
        util = alloc.get_utilization("grid_trading_v1")
        assert util["current_stake_used"] == pytest.approx(500.0)

    def test_resumption_restores_trading(self) -> None:
        alloc = _make_allocator(max_open_trades=5, stake_pct=0.5)
        alloc.record_suspension("grid_trading_v1")
        alloc.record_resumption("grid_trading_v1")
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is True

    def test_double_suspension_is_idempotent(self) -> None:
        alloc = _make_allocator()
        alloc.record_suspension("grid_trading_v1")
        alloc.record_suspension("grid_trading_v1")  # should not raise
        util = alloc.get_utilization("grid_trading_v1")
        assert util["is_suspended"] is True

    def test_rejection_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        alloc = _make_allocator(max_open_trades=1, stake_pct=1.0)
        alloc.record_open("grid_trading_v1", 100.0)
        with caplog.at_level(logging.WARNING, logger="algo_system.budget"):
            alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert any("trade_quota_exceeded" in r.message for r in caplog.records)


# ===========================================================================
# Utilization introspection
# ===========================================================================

class TestBudgetUtilization:
    def test_get_utilization_keys(self) -> None:
        alloc = _make_allocator()
        util = alloc.get_utilization("grid_trading_v1")
        for key in (
            "module_id", "max_open_trades", "current_open_trades",
            "trade_slots_free", "stake_allocation_pct",
            "current_stake_used", "is_suspended",
        ):
            assert key in util

    def test_trade_slots_free_decrements(self) -> None:
        alloc = _make_allocator(max_open_trades=5, stake_pct=1.0)
        assert alloc.get_utilization("grid_trading_v1")["trade_slots_free"] == 5
        alloc.record_open("grid_trading_v1", 100.0)
        assert alloc.get_utilization("grid_trading_v1")["trade_slots_free"] == 4

    def test_close_clamps_to_zero(self) -> None:
        alloc = _make_allocator()
        # record_close when count is already 0 should clamp, not go negative
        alloc.record_close("grid_trading_v1", 0.0)
        util = alloc.get_utilization("grid_trading_v1")
        assert util["current_open_trades"] == 0
        assert util["current_stake_used"] == pytest.approx(0.0)


# ===========================================================================
# Regression tests for bugs found by adversarial bug hunt
# ===========================================================================


class TestBudgetAllocatorNegativeStake:
    """BUG FIX: negative proposed_stake must be rejected, not bypass cap check."""

    def test_negative_stake_rejected(self) -> None:
        alloc = _make_allocator(stake_pct=1.0)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", -100.0, 10_000.0)
        assert allowed is False
        assert "invalid_stake" in reason

    def test_zero_stake_rejected(self) -> None:
        alloc = _make_allocator(stake_pct=1.0)
        allowed, reason = alloc.can_open_trade("grid_trading_v1", 0.0, 10_000.0)
        assert allowed is False
        assert "invalid_stake" in reason

    def test_positive_stake_still_passes_when_allowed(self) -> None:
        alloc = _make_allocator(stake_pct=1.0)
        allowed, _ = alloc.can_open_trade("grid_trading_v1", 100.0, 10_000.0)
        assert allowed is True


class TestBudgetAllocatorGetAllBudgetsCopied:
    """BUG FIX: get_all_budgets must return copies, not live mutable references."""

    def test_mutating_returned_budget_does_not_affect_internal_state(self) -> None:
        alloc = _make_allocator(max_open_trades=5, stake_pct=0.5)
        alloc.record_open("grid_trading_v1", 100.0)

        budgets = alloc.get_all_budgets()
        # Mutate the returned copy
        budgets["grid_trading_v1"].current_open_trades = 999
        budgets["grid_trading_v1"].current_stake_used = 999.0

        # Internal state must be unchanged
        util = alloc.get_utilization("grid_trading_v1")
        assert util["current_open_trades"] == 1
        assert util["current_stake_used"] == pytest.approx(100.0)


# ===========================================================================
# T041 — PairSelector integration via ModuleRegistry
# ===========================================================================


class TestPairSelector:
    """T041 — PairSelector integration via ModuleRegistry."""

    def test_pair_selector_not_configured_by_default(self) -> None:
        registry = ModuleRegistry(AlgoLifecycleSM())
        result = registry.get_active_pairs_for_module("any")
        assert isinstance(result, list)
        # No modules registered and no selector → returns list of module keys (empty)
        assert result == []

    def test_configure_pair_selector_enabled(self) -> None:
        registry = ModuleRegistry(AlgoLifecycleSM())
        registry.configure_pair_selector(
            {"enabled": True, "evaluation_interval_candles": 1}
        )
        assert registry._pair_selector is not None

    def test_configure_pair_selector_disabled(self) -> None:
        registry = ModuleRegistry(AlgoLifecycleSM())
        registry.configure_pair_selector({"enabled": False})
        assert registry._pair_selector is None

    def test_update_active_pairs_with_flat_df(self) -> None:
        registry = ModuleRegistry(AlgoLifecycleSM())
        registry.configure_pair_selector(
            {"enabled": True, "evaluation_interval_candles": 1}
        )
        flat_df = make_ohlcv_df(n_candles=100, volatility=0.0)
        # Should not raise
        registry.update_active_pairs(
            "grid_trading_v1",
            ["BTC/USDT"],
            {"BTC/USDT": flat_df},
        )
        result = registry.get_active_pairs_for_module("grid_trading_v1")
        assert isinstance(result, list)

    def test_no_selector_returns_empty_list(self) -> None:
        registry = ModuleRegistry(AlgoLifecycleSM())
        # No modules registered, no selector
        result = registry.get_active_pairs_for_module("x")
        assert result == []


# ===========================================================================
# T045 — ModuleResolver discovers IAlgoModule subclasses from a temp directory
# ===========================================================================


class TestModuleDiscovery:
    """T045 — ModuleResolver discovers IAlgoModule subclasses from a temp directory."""

    def test_discovers_stub_module_from_temp_dir(self) -> None:
        strat_root = str(Path(__file__).resolve().parents[2])
        stub_code = (
            "import sys\n"
            f"sys.path.insert(0, {strat_root!r})\n"
            "from algo_system.base.ialgo_module import ("
            "IAlgoModule, ModuleCapability, ModuleSignal, ModuleState)\n"
            "from algo_system.base.module_context import ModuleContext\n"
            "from pandas import DataFrame\n\n"
            "class TestStubModule(IAlgoModule):\n"
            '    module_id = "test_stub_v1"\n'
            '    version = "0.1.0"\n'
            "    supports_backtest = ModuleCapability.SUPPORTED\n"
            "    supports_paper = ModuleCapability.SUPPORTED\n"
            "    supports_live = ModuleCapability.UNSUPPORTED\n"
            "    supports_hyperopt = ModuleCapability.UNSUPPORTED\n"
            "    supports_short = ModuleCapability.UNSUPPORTED\n"
            "    supports_position_adjust = ModuleCapability.UNSUPPORTED\n"
            "    def initialize(self, ctx): pass\n"
            "    def on_bot_start(self, ctx): pass\n"
            "    def shutdown(self, ctx): pass\n"
            "    def get_module_state(self): return ModuleState.ACTIVE\n"
            "    def reset_module_state(self): pass\n"
            "    def populate_indicators(self, df, metadata, ctx): return df\n"
            "    def generate_entry_signal(self, df, metadata, ctx): return ModuleSignal()\n"
            "    def generate_exit_signal(self, df, metadata, ctx): return ModuleSignal()\n"
            "    def adjust_position(self, trade, t, r, p, mn, mx, ctx): return None\n"
        )
        temp_dir = tempfile.mkdtemp()
        stub_path = temp_dir + "/test_stub_module.py"
        with open(stub_path, "w") as f:
            f.write(stub_code)

        discovered = ModuleResolver().discover_modules(temp_dir)
        module_ids = [cls.module_id for cls in discovered]
        assert "test_stub_v1" in module_ids

    def test_resolver_ignores_non_module_files(self) -> None:
        temp_dir = tempfile.mkdtemp()
        non_module_path = temp_dir + "/not_a_module.py"
        with open(non_module_path, "w") as f:
            f.write("class Foo:\n    pass\n")

        discovered = ModuleResolver().discover_modules(temp_dir)
        assert discovered == []

    def test_3_failures_suspends_module(self) -> None:
        lifecycle = AlgoLifecycleSM()
        lifecycle.register_module("grid_trading_v1")
        # Transition to ACTIVE so failures can accumulate toward SUSPENDED
        lifecycle.transition("grid_trading_v1", "BTC/USDT", ModuleState.ACTIVE)

        count1 = lifecycle.record_failure("grid_trading_v1", "BTC/USDT")
        count2 = lifecycle.record_failure("grid_trading_v1", "BTC/USDT")
        count3 = lifecycle.record_failure("grid_trading_v1", "BTC/USDT")

        assert count1 == 1
        assert count2 == 2
        assert count3 == 3
        assert lifecycle.get_failure_count("grid_trading_v1", "BTC/USDT") == 3

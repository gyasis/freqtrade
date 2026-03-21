"""
tests/test_reasoning.py
Unit tests for RuleBasedReasoningEngine, RoutingDecision, and EntryQualityEvaluator.

No talib imports — all tests work with pure-Python DataFrames.
The reasoning engine gracefully handles missing talib via try/except blocks.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

# ---------------------------------------------------------------------------
# sys.path setup — mirrors test_grid_module.py
# ---------------------------------------------------------------------------
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
_TESTS_DIR = Path(__file__).resolve().parent       # algo_system/tests/
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.reasoning.rule_based_reasoning import RuleBasedReasoningEngine
from algo_system.reasoning.reasoning_interface import RoutingDecision
from algo_system.reasoning.entry_quality_evaluator import EntryQualityEvaluator

from test_helpers import make_ohlcv_df, make_ranging_ohlcv_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(activation_threshold: float = 0.6) -> RuleBasedReasoningEngine:
    """Return an initialized RuleBasedReasoningEngine."""
    engine = RuleBasedReasoningEngine()
    engine.initialize(
        config={"activation_threshold": activation_threshold},
        active_module_ids=["grid_trading_v1"],
    )
    return engine


def _make_midpoint_df(n_candles: int = 50) -> DataFrame:
    """
    Build a DataFrame where the last close is exactly at the midpoint
    of the high/low range of the last N candles.
    """
    center = 100.0
    highs = np.full(n_candles, center + 10.0)
    lows = np.full(n_candles, center - 10.0)
    # Close exactly at midpoint = center
    closes = np.full(n_candles, center)
    opens = closes.copy()
    volumes = np.ones(n_candles) * 1000.0
    index = pd.date_range(end="2024-01-01", periods=n_candles, freq="1h")
    return DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


def _make_high_momentum_df(n_candles: int = 50, lookback: int = 10) -> DataFrame:
    """
    Build a DataFrame where the last candle is ~10% above lookback candles ago.
    This should yield HIGH momentum → evaluator penalizes score.
    """
    base_price = 100.0
    closes = np.full(n_candles, base_price)
    # Last candle is 10% above the one at -lookback-1
    closes[-1] = base_price * 1.10
    highs = closes * 1.001
    lows = closes * 0.999
    opens = closes.copy()
    volumes = np.ones(n_candles) * 1000.0
    index = pd.date_range(end="2024-01-01", periods=n_candles, freq="1h")
    return DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


def _make_low_momentum_df(n_candles: int = 50) -> DataFrame:
    """
    Build a DataFrame where the last candle has minimal price change from lookback ago.
    """
    base_price = 100.0
    closes = np.full(n_candles, base_price)
    highs = closes * 1.001
    lows = closes * 0.999
    opens = closes.copy()
    volumes = np.ones(n_candles) * 1000.0
    index = pd.date_range(end="2024-01-01", periods=n_candles, freq="1h")
    return DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


# ===========================================================================
# TestRuleBasedReasoningEngineScore
# ===========================================================================

class TestRuleBasedReasoningEngineScore:

    def test_score_returns_dict_with_module_id(self) -> None:
        """score_modules returns a dict containing 'grid_trading_v1' key."""
        engine = _make_engine()
        df = make_ranging_ohlcv_df(n_candles=100)
        result = engine.score_modules("BTC/USDT", df, ["grid_trading_v1"])
        assert isinstance(result, dict)
        assert "grid_trading_v1" in result

    def test_score_range_0_to_1(self) -> None:
        """Score for grid_trading_v1 is between 0.0 and 1.0 inclusive."""
        engine = _make_engine()
        df = make_ranging_ohlcv_df(n_candles=100)
        result = engine.score_modules("BTC/USDT", df, ["grid_trading_v1"])
        score = result["grid_trading_v1"]
        assert 0.0 <= score <= 1.0

    def test_unknown_module_scores_zero(self) -> None:
        """An unknown module_id always returns 0.0."""
        engine = _make_engine()
        df = make_ranging_ohlcv_df(n_candles=100)
        result = engine.score_modules("BTC/USDT", df, ["unknown_module"])
        assert result["unknown_module"] == pytest.approx(0.0)

    def test_insufficient_data_returns_zero(self) -> None:
        """A DataFrame with only 5 rows → grid_trading_v1 score == 0.0."""
        engine = _make_engine()
        df = make_ohlcv_df(n_candles=5)
        result = engine.score_modules("BTC/USDT", df, ["grid_trading_v1"])
        assert result["grid_trading_v1"] == pytest.approx(0.0)

    def test_score_with_multiple_modules(self) -> None:
        """Passing two module IDs → both appear as keys in the result dict."""
        engine = _make_engine()
        df = make_ranging_ohlcv_df(n_candles=100)
        result = engine.score_modules("BTC/USDT", df, ["grid_trading_v1", "other_mod"])
        assert "grid_trading_v1" in result
        assert "other_mod" in result


# ===========================================================================
# TestRuleBasedReasoningEngineRouting
# ===========================================================================

class TestRuleBasedReasoningEngineRouting:

    def test_returns_routing_decision_when_score_qualifies(self) -> None:
        """
        With activation_threshold=0.0 any non-zero score qualifies.
        Result must be a RoutingDecision with authoritative_module_id=='grid_trading_v1'.
        If talib is absent all talib components fail gracefully, but since threshold=0.0
        the engine will still return a decision (even for score=0.0 on a 100-row df).

        Note: score_modules is called internally; with no talib, score==0.0.
        We set threshold=0.0 so 0.0 >= 0.0 passes.
        """
        engine = _make_engine(activation_threshold=0.0)
        df = make_ranging_ohlcv_df(n_candles=100)
        decision = engine.get_routing_decision("BTC/USDT", df, ["grid_trading_v1"])
        assert decision is not None
        assert decision.authoritative_module_id == "grid_trading_v1"

    def test_returns_none_when_score_below_threshold(self) -> None:
        """
        activation_threshold=1.0 is impossible to reach without talib.
        get_routing_decision must return None.
        """
        engine = _make_engine(activation_threshold=1.0)
        df = make_ranging_ohlcv_df(n_candles=100)
        decision = engine.get_routing_decision("BTC/USDT", df, ["grid_trading_v1"])
        assert decision is None

    def test_reuses_valid_decision_before_ttl(self) -> None:
        """
        A non-expired current_decision is returned as-is (tick decrements counter).
        The returned decision's authoritative_module_id matches the original.
        """
        engine = _make_engine(activation_threshold=0.0)
        df = make_ranging_ohlcv_df(n_candles=100)

        # Obtain initial decision
        first = engine.get_routing_decision("BTC/USDT", df, ["grid_trading_v1"])
        assert first is not None

        # Ensure it is not expired (candles_remaining > 0)
        assert not first.is_expired()

        # Second call with valid current_decision → same module returned
        second = engine.get_routing_decision(
            "BTC/USDT", df, ["grid_trading_v1"], current_decision=first
        )
        assert second is not None
        assert second.authoritative_module_id == first.authoritative_module_id

    def test_expired_decision_triggers_reeval(self) -> None:
        """
        A RoutingDecision with candles_remaining=0 is expired; the engine must
        re-evaluate rather than returning the stale decision unchanged.
        """
        engine = _make_engine(activation_threshold=0.0)
        df = make_ranging_ohlcv_df(n_candles=100)

        expired = RoutingDecision(
            authoritative_module_id="grid_trading_v1",
            confidence=0.99,
            rationale="old decision",
            valid_for_candles=1,
            candles_remaining=0,
        )
        assert expired.is_expired()

        # Engine must NOT return the expired decision unchanged —
        # it re-evaluates and returns a new decision (or None).
        result = engine.get_routing_decision(
            "BTC/USDT", df, ["grid_trading_v1"], current_decision=expired
        )
        # The returned decision must not be the exact same stale object
        # (it either produced a fresh one or None)
        if result is not None:
            assert result is not expired or result.candles_remaining > 0

    def test_routing_decision_has_rationale(self) -> None:
        """With activation_threshold=0.0, the returned decision has a non-empty rationale."""
        engine = _make_engine(activation_threshold=0.0)
        df = make_ranging_ohlcv_df(n_candles=100)
        decision = engine.get_routing_decision("BTC/USDT", df, ["grid_trading_v1"])
        assert decision is not None
        assert isinstance(decision.rationale, str)
        assert len(decision.rationale) > 0


# ===========================================================================
# TestRoutingDecisionTTL
# ===========================================================================

class TestRoutingDecisionTTL:

    def _make_decision(self, candles_remaining: int = 5) -> RoutingDecision:
        return RoutingDecision(
            authoritative_module_id="grid_trading_v1",
            confidence=0.8,
            rationale="test decision",
            valid_for_candles=10,
            candles_remaining=candles_remaining,
        )

    def test_is_expired_false_when_candles_remain(self) -> None:
        """candles_remaining=5 → is_expired() is False."""
        decision = self._make_decision(candles_remaining=5)
        assert decision.is_expired() is False

    def test_is_expired_true_when_zero(self) -> None:
        """candles_remaining=0 → is_expired() is True."""
        decision = self._make_decision(candles_remaining=0)
        assert decision.is_expired() is True

    def test_tick_decrements_counter(self) -> None:
        """tick() decrements candles_remaining by 1."""
        decision = self._make_decision(candles_remaining=5)
        decision.tick()
        assert decision.candles_remaining == 4

    def test_tick_does_not_go_below_zero(self) -> None:
        """tick() on a decision with candles_remaining=0 stays at 0."""
        decision = self._make_decision(candles_remaining=0)
        decision.tick()
        assert decision.candles_remaining == 0


# ===========================================================================
# TestEntryQualityEvaluator
# ===========================================================================

class TestEntryQualityEvaluator:

    def _make_evaluator(self, **kwargs) -> EntryQualityEvaluator:
        config = {
            "momentum_lookback_candles": 10,
            "momentum_threshold": 0.03,
            "entry_quality_threshold": 0.5,
            "max_defer_candles": 20,
        }
        config.update(kwargs)
        return EntryQualityEvaluator(config)

    def test_score_returns_tuple(self) -> None:
        """score() returns a 2-tuple (float, str)."""
        ev = self._make_evaluator()
        df = make_ranging_ohlcv_df(n_candles=50)
        result = ev.score("BTC/USDT", df)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], float)
        assert isinstance(result[1], str)

    def test_score_range_0_to_1(self) -> None:
        """score is in [0.0, 1.0]."""
        ev = self._make_evaluator()
        df = make_ranging_ohlcv_df(n_candles=50)
        score, _ = ev.score("BTC/USDT", df)
        assert 0.0 <= score <= 1.0

    def test_insufficient_data_returns_zero(self) -> None:
        """DataFrame with 2 rows → score == 0.0 (needs lookback+1=11 rows)."""
        ev = self._make_evaluator()
        df = make_ohlcv_df(n_candles=2)
        score, rationale = ev.score("BTC/USDT", df)
        assert score == pytest.approx(0.0)
        assert "insufficient_data" in rationale

    def test_high_momentum_reduces_score(self) -> None:
        """
        A df where current close is 10% above lookback candles ago yields a
        lower score than a flat/low-momentum df.
        """
        ev = self._make_evaluator()
        high_mom_df = _make_high_momentum_df(n_candles=50, lookback=10)
        low_mom_df = _make_low_momentum_df(n_candles=50)

        high_score, _ = ev.score("BTC/USDT", high_mom_df)
        low_score, _ = ev.score("BTC/USDT", low_mom_df)

        assert high_score < low_score, (
            f"High-momentum score ({high_score}) should be less than "
            f"low-momentum score ({low_score})"
        )

    def test_near_midpoint_boosts_score(self) -> None:
        """
        A df where current close is exactly at the midpoint of high/low range
        should yield score >= 0.5 (the +0.5 midpoint component fires).
        """
        ev = self._make_evaluator()
        df = _make_midpoint_df(n_candles=50)
        score, _ = ev.score("BTC/USDT", df)
        assert score >= 0.5, f"Near-midpoint score should be >= 0.5, got {score}"

    def test_quality_threshold_gate(self) -> None:
        """
        Default threshold=0.5: a 100-candle ranging df at midpoint passes.
        Artificially high threshold=1.1 always returns False.
        """
        # Default threshold — should pass for a low-momentum midpoint df
        ev_default = self._make_evaluator()
        df = _make_midpoint_df(n_candles=50)
        qualifies, _ = ev_default.is_quality_entry("BTC/USDT", df, defer_count=0)
        assert qualifies is True

        # Threshold above max possible score (1.0) — always False
        ev_high = self._make_evaluator(entry_quality_threshold=1.1)
        qualifies_high, _ = ev_high.is_quality_entry("BTC/USDT", df, defer_count=0)
        assert qualifies_high is False

    def test_defer_count_warning_logged(self, caplog) -> None:
        """
        When defer_count > max_defer_candles, a WARNING is emitted.
        """
        ev = self._make_evaluator(max_defer_candles=20)
        df = make_ranging_ohlcv_df(n_candles=50)

        with caplog.at_level(logging.WARNING, logger="algo_system.entry_quality"):
            ev.is_quality_entry("BTC/USDT", df, defer_count=25)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_messages) > 0, "Expected at least one WARNING log for defer overflow"

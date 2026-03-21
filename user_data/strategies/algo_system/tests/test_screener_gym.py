"""
tests/test_screener_gym.py
==========================
Tests for ScreenerGym and ScreenerWeightsOptimizer.

All EODHD API calls are mocked — no real HTTP requests.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
from typing import Dict

import pandas as pd
import pytest

from ..orchestrator.screener_gym import (
    ScoringWeights,
    ScreenerGym,
    WEIGHT_FIELDS,
)
from ..orchestrator.screener_weights_optimizer import (
    ScreenerWeightsOptimizer,
    SQLiteWeightsFallback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_price_series(
    dates: pd.DatetimeIndex, start: float = 100.0
) -> pd.Series:
    """Return a monotonically increasing close price series."""
    return pd.Series(
        [start + i for i in range(len(dates))], index=dates, name="close"
    )


def _minimal_history_df(n: int = 20) -> pd.DataFrame:
    """Build a minimal history DataFrame with all required columns."""
    dates = pd.date_range("2023-01-03", periods=n, freq="B")
    closes = _make_price_series(dates)
    df = pd.DataFrame(
        {
            "close": closes,
            "sma50": closes * 0.95,
            "sma200": closes * 0.90,
            "ema12": closes * 1.01,
            "ema26": closes * 0.99,
            "atr14": closes * 0.02,
            "adx14": [30.0] * n,
            "atr14_pcnt": [2.0] * n,
            "ema_cross": [True] * n,
            "above_sma50": [True] * n,
            "above_sma200": [True] * n,
            "adx_strong": [True] * n,
            "adx_very_strong": [False] * n,
            "forward_5d": [0.02] * n,  # 2% forward return
        },
        index=dates,
    )
    return df


# ---------------------------------------------------------------------------
# TestScoringWeights
# ---------------------------------------------------------------------------


class TestScoringWeights:
    def test_to_dict_roundtrip(self):
        """from_dict(w.to_dict()) should reproduce an identical ScoringWeights."""
        w = ScoringWeights(
            w_bull_market=1.5,
            w_next_action=0.8,
            w_ema_cross=1.2,
            w_above_sma50=0.3,
            w_above_sma200=0.4,
            w_atr_volatile=0.9,
            w_adx_strong=1.1,
            w_adx_very_strong=0.2,
        )
        assert ScoringWeights.from_dict(w.to_dict()) == w

    def test_default_weights_positive(self):
        """All default weight values should be non-negative."""
        w = ScoringWeights()
        for field_name in WEIGHT_FIELDS:
            val = getattr(w, field_name)
            assert val >= 0.0, f"{field_name} default weight is negative: {val}"

    def test_from_dict_ignores_unknown_keys(self):
        """Extra keys in the dict must not raise an exception."""
        d = {
            "w_bull_market": 2.0,
            "w_next_action": 1.0,
            "w_ema_cross": 1.0,
            "w_above_sma50": 0.5,
            "w_above_sma200": 0.5,
            "w_atr_volatile": 1.0,
            "w_adx_strong": 1.5,
            "w_adx_very_strong": 0.5,
            "unknown_future_field": 99.9,
            "another_unknown": "ignored",
        }
        w = ScoringWeights.from_dict(d)
        assert w.w_bull_market == 2.0
        # Only known fields are mapped; no AttributeError
        assert not hasattr(w, "unknown_future_field")

    def test_from_optuna_trial_mock(self):
        """ScoringWeights.from_optuna_trial should call suggest_float for each field."""
        trial = MagicMock()
        # Configure mock to return specific values per field
        return_values: Dict[str, float] = {
            "w_bull_market": 1.5,
            "w_next_action": 0.7,
            "w_ema_cross": 0.9,
            "w_above_sma50": 0.3,
            "w_above_sma200": 0.4,
            "w_atr_volatile": 1.2,
            "w_adx_strong": 1.8,
            "w_adx_very_strong": 0.6,
        }
        trial.suggest_float.side_effect = lambda name, lo, hi: return_values[name]

        w = ScoringWeights.from_optuna_trial(trial)

        # Verify all weight fields were suggested
        assert trial.suggest_float.call_count == len(WEIGHT_FIELDS)
        for field_name in WEIGHT_FIELDS:
            assert getattr(w, field_name) == return_values[field_name]


# ---------------------------------------------------------------------------
# TestScreenerGym
# ---------------------------------------------------------------------------


class TestScreenerGym:
    def _gym_with_history(self, symbols=("AAPL", "TSLA", "MSFT")) -> ScreenerGym:
        """Return a ScreenerGym with synthetic in-memory history (no API calls)."""
        gym = ScreenerGym(api_key="test-key")
        for sym in symbols:
            gym._history[sym] = _minimal_history_df(n=20)
        # Build eval dates from the first symbol's index
        gym._eval_dates = list(gym._history[symbols[0]].index)
        return gym

    def test_evaluate_returns_float(self):
        """evaluate() must return a float when history is loaded."""
        gym = self._gym_with_history()
        result = gym.evaluate(ScoringWeights(), top_n=2, hold_days=5)
        assert isinstance(result, float)
        # Our synthetic data has 2% forward returns — result should be positive
        assert result > 0.0

    def test_evaluate_empty_history_returns_sentinel(self):
        """evaluate() returns -999.0 when no history has been loaded."""
        gym = ScreenerGym(api_key="test-key")
        result = gym.evaluate(ScoringWeights())
        assert result == -999.0

    def test_score_symbol_on_date_known_date(self):
        """score_symbol_on_date computes a positive score for a bullish row."""
        gym = self._gym_with_history(symbols=("AAPL",))
        date = gym._eval_dates[5]
        score = gym.score_symbol_on_date("AAPL", date, ScoringWeights())
        # The synthetic history has ema_cross=True, above_sma50=True,
        # above_sma200=True, adx_strong=True — expect multiple weight contributions
        assert score > 0.0

    def test_score_symbol_on_date_unknown_date(self):
        """score_symbol_on_date returns 0.0 for a date not in history."""
        gym = self._gym_with_history(symbols=("AAPL",))
        missing_date = pd.Timestamp("1990-01-01")
        score = gym.score_symbol_on_date("AAPL", missing_date, ScoringWeights())
        assert score == 0.0

    def test_higher_weights_select_different_stocks(self):
        """Changing weights changes which symbols are top-ranked."""
        gym = ScreenerGym(api_key="test-key")

        # AAPL: strong ADX, weak EMA cross
        dates = pd.date_range("2023-01-03", periods=10, freq="B")
        aapl_df = _minimal_history_df(n=10)
        aapl_df["adx_strong"] = True
        aapl_df["adx_very_strong"] = True
        aapl_df["ema_cross"] = False
        aapl_df["above_sma50"] = False
        aapl_df["above_sma200"] = False
        gym._history["AAPL"] = aapl_df

        # TSLA: strong EMA cross, weak ADX
        tsla_df = _minimal_history_df(n=10)
        tsla_df["adx_strong"] = False
        tsla_df["adx_very_strong"] = False
        tsla_df["ema_cross"] = True
        tsla_df["above_sma50"] = True
        tsla_df["above_sma200"] = True
        gym._history["TSLA"] = tsla_df

        gym._eval_dates = list(aapl_df.index)
        date = gym._eval_dates[0]

        # Weights that heavily favour ADX
        adx_weights = ScoringWeights(
            w_bull_market=0.0,
            w_next_action=0.0,
            w_ema_cross=0.0,
            w_above_sma50=0.0,
            w_above_sma200=0.0,
            w_atr_volatile=0.0,
            w_adx_strong=3.0,
            w_adx_very_strong=2.0,
        )
        # Weights that heavily favour EMA cross / trend
        ema_weights = ScoringWeights(
            w_bull_market=3.0,
            w_next_action=2.0,
            w_ema_cross=2.0,
            w_above_sma50=1.5,
            w_above_sma200=1.5,
            w_atr_volatile=0.0,
            w_adx_strong=0.0,
            w_adx_very_strong=0.0,
        )

        score_aapl_adx = gym.score_symbol_on_date("AAPL", date, adx_weights)
        score_tsla_adx = gym.score_symbol_on_date("TSLA", date, adx_weights)
        assert score_aapl_adx > score_tsla_adx, "ADX weights should prefer AAPL"

        score_aapl_ema = gym.score_symbol_on_date("AAPL", date, ema_weights)
        score_tsla_ema = gym.score_symbol_on_date("TSLA", date, ema_weights)
        assert score_tsla_ema > score_aapl_ema, "EMA weights should prefer TSLA"


# ---------------------------------------------------------------------------
# TestScreenerWeightsOptimizer
# ---------------------------------------------------------------------------


class TestScreenerWeightsOptimizer:
    def test_get_best_weights_missing_db(self, tmp_path):
        """get_best_weights returns None when the database file does not exist."""
        db_path = tmp_path / "nonexistent.db"
        opt = ScreenerWeightsOptimizer(db_path=db_path, api_key="key")
        result = opt.get_best_weights()
        assert result is None

    def test_inject_into_shared_state(self, tmp_path):
        """inject_into_shared_state calls shared_state.set with the weights dict."""
        opt = ScreenerWeightsOptimizer(
            db_path=tmp_path / "weights.db", api_key="key"
        )
        # Patch get_best_weights to return known weights
        expected = ScoringWeights(w_bull_market=1.7)
        with patch.object(opt, "get_best_weights", return_value=expected):
            shared_state = MagicMock()
            opt.inject_into_shared_state(shared_state, module_id="stock_screener")
            shared_state.set.assert_called_once_with(
                "stock_screener", "scoring_weights", expected.to_dict()
            )

    def test_run_mock_optuna(self, tmp_path):
        """run() uses Optuna study and converts best_params to ScoringWeights."""
        db_path = tmp_path / "test_run.db"
        opt = ScreenerWeightsOptimizer(db_path=db_path, api_key="test-key")

        best_params = {
            "w_bull_market": 1.5,
            "w_next_action": 0.8,
            "w_ema_cross": 1.1,
            "w_above_sma50": 0.4,
            "w_above_sma200": 0.6,
            "w_atr_volatile": 1.3,
            "w_adx_strong": 1.9,
            "w_adx_very_strong": 0.7,
        }

        # Build a minimal mock study whose optimize() is a no-op
        mock_study = MagicMock()
        mock_study.best_params = best_params
        mock_study.best_value = 0.05

        # Build a mock gym class that returns a lightweight instance
        mock_gym_instance = MagicMock()
        mock_gym_instance.evaluate.return_value = 0.05
        mock_gym_instance.summary.return_value = {"symbols_loaded": 2, "eval_dates": 10}
        mock_gym_instance.load_history.return_value = None

        mock_optuna_mod = MagicMock()
        mock_optuna_mod.create_study.return_value = mock_study
        mock_optuna_mod.logging.WARNING = 30

        # Patch directly on the optimizer's run method's module globals.
        # opt.run lives in ScreenerWeightsOptimizer which is in the swo module;
        # we patch via the method's __globals__ dict to be immune to import path
        # differences between pytest's relative import and absolute import keys.
        run_globals = opt.run.__func__.__globals__
        orig_gym = run_globals["ScreenerGym"]
        orig_optuna = run_globals.get("optuna")
        orig_flag = run_globals.get("_OPTUNA_AVAILABLE")

        MockGymClass = MagicMock(return_value=mock_gym_instance)

        run_globals["ScreenerGym"] = MockGymClass
        run_globals["optuna"] = mock_optuna_mod
        run_globals["_OPTUNA_AVAILABLE"] = True
        try:
            result = opt.run(
                symbols=["AAPL", "TSLA"],
                date_from="2022-01-01",
                date_to="2023-12-31",
                n_trials=5,
            )
        finally:
            run_globals["ScreenerGym"] = orig_gym
            run_globals["optuna"] = orig_optuna
            run_globals["_OPTUNA_AVAILABLE"] = orig_flag

        assert isinstance(result, ScoringWeights)
        assert result.w_bull_market == pytest.approx(1.5)
        assert result.w_adx_strong == pytest.approx(1.9)
        mock_study.optimize.assert_called_once()

    def test_sqlite_fallback_empty_db(self, tmp_path):
        """SQLiteWeightsFallback returns None when the study has no trials."""
        db_path = tmp_path / "empty.db"

        # Create a minimal valid Optuna-style schema with a study but no trials
        with sqlite3.connect(str(db_path)) as con:
            con.executescript(
                """
                CREATE TABLE studies (
                    study_id INTEGER PRIMARY KEY,
                    study_name TEXT NOT NULL
                );
                CREATE TABLE trials (
                    trial_id INTEGER PRIMARY KEY,
                    study_id INTEGER,
                    state TEXT,
                    value REAL
                );
                CREATE TABLE trial_params (
                    trial_id INTEGER,
                    param_name TEXT,
                    param_value REAL,
                    distribution_json TEXT
                );
                INSERT INTO studies (study_name) VALUES ('screener_weights');
                """
            )

        fb = SQLiteWeightsFallback(db_path)
        result = fb.get_best_weights("screener_weights")
        assert result is None

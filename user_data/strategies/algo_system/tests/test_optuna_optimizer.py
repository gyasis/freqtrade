"""
tests/test_optuna_optimizer.py
================================
Unit tests for OptunaGridOptimizer and SQLiteOptunaFallback.

All tests create a minimal Optuna-compatible SQLite schema via plain
``sqlite3`` so that the test suite has no ``optuna`` dependency.  The tests
exercise both the direct-optuna path (when optuna is installed) and the
SQLite fallback path (via import mock).

Fixtures
--------
- ``tmp_db`` — a ``pathlib.Path`` pointing to a temporary SQLite file that
  has been populated with one study per SYMBOL_YEAR in ``STUDY_FIXTURES``.
- ``empty_db`` — a valid SQLite file with the Optuna schema tables but no
  studies or trials.

Test matrix
-----------
- test_loads_empty_db
- test_get_best_config_returns_none_for_unknown_symbol
- test_get_best_config_returns_gridconfigv2
- test_get_best_config_prefers_requested_year
- test_get_best_config_falls_back_to_best_value_when_year_missing
- test_get_all_symbols
- test_get_study_summary_has_correct_columns
- test_inject_into_shared_state
- test_inject_does_not_crash_when_symbol_has_no_best_params
- test_sqlite_fallback_when_optuna_missing
- test_load_symbol_filter
- test_strip_exchange_suffix
- test_db_not_found_returns_empty
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — make algo_system importable without installing freqtrade
# ---------------------------------------------------------------------------

_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
if str(_STRAT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STRAT_ROOT))

from algo_system.config.grid_config import GridConfig
from algo_system.orchestrator.optuna_grid_optimizer import (
    OptunaGridOptimizer,
    SQLiteOptunaFallback,
    _strip_exchange_suffix,
)
from algo_system.orchestrator.shared_state import SharedState

# ---------------------------------------------------------------------------
# Minimal Optuna SQLite schema builder
# ---------------------------------------------------------------------------

# Optuna stores params in trial_params.param_value as floats; the
# distribution_json column tells us how to coerce them back.
# We use the simplest representations here.

_FLOAT_DIST = json.dumps({"name": "FloatDistribution", "low": 0.0, "high": 1.0})
_INT_DIST = json.dumps({"name": "IntDistribution", "low": 1, "high": 1000})
_CATEGORICAL_DIST_METHOD = json.dumps(
    {
        "name": "CategoricalDistribution",
        "choices": ["moving_average", "vwap"],
    }
)
_CATEGORICAL_DIST_ALLOC = json.dumps(
    {
        "name": "CategoricalDistribution",
        "choices": ["fixed_pct", "proportional", "volatility"],
    }
)


def _create_schema(con: sqlite3.Connection) -> None:
    """Create the minimal Optuna SQLite tables."""
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS studies (
            study_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            study_name TEXT    UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trials (
            trial_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            study_id  INTEGER NOT NULL,
            state     TEXT    NOT NULL DEFAULT 'COMPLETE',
            value     REAL
        );

        CREATE TABLE IF NOT EXISTS trial_params (
            param_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trial_id          INTEGER NOT NULL,
            param_name        TEXT    NOT NULL,
            param_value       REAL    NOT NULL,
            distribution_json TEXT
        );
        """
    )


# ---------------------------------------------------------------------------
# Study fixture data
# ---------------------------------------------------------------------------

# Each entry: (symbol, year, best_value, params)
# We model a 2-trial study where trial 1 is worse and trial 2 is best.
STUDY_FIXTURES: List[tuple] = [
    (
        "TSLA",
        2023,
        -0.85,
        {
            "grid_distance": 0.004,
            "grid_range": 0.08,
            "history_lookback": 40,
            "method": "moving_average",   # index 0 in choices
            "period": 14,
            "dynamic_midprice": 1.0,  # stored as float; truthy
            "allocation_strategy": "fixed_pct",  # index 0 in choices
            "allocation_param": 1.5,
            "volatility_threshold": 0.025,
            "deviation_threshold": 0.015,
            "auto_set_grid": 0.0,
        },
    ),
    (
        "TSLA",
        2022,
        -0.70,
        {
            "grid_distance": 0.006,
            "grid_range": 0.12,
            "history_lookback": 30,
            "method": "moving_average",
            "period": 21,
            "dynamic_midprice": 0.0,
            "allocation_strategy": "fixed_pct",
            "allocation_param": 1.0,
            "volatility_threshold": 0.03,
            "deviation_threshold": 0.02,
            "auto_set_grid": 0.0,
        },
    ),
    (
        "AMZN",
        2023,
        -0.90,
        {
            "grid_distance": 0.003,
            "grid_range": 0.06,
            "history_lookback": 60,
            "method": "moving_average",
            "period": 10,
            "dynamic_midprice": 1.0,
            "allocation_strategy": "fixed_pct",
            "allocation_param": 2.0,
            "volatility_threshold": 0.02,
            "deviation_threshold": 0.01,
            "auto_set_grid": 1.0,
        },
    ),
]


def _insert_study(
    con: sqlite3.Connection,
    symbol: str,
    year: int,
    best_value: float,
    params: Dict[str, Any],
    *,
    add_bad_trial: bool = True,
) -> None:
    """Insert one study with two trials into *con*.

    Parameters
    ----------
    con : sqlite3.Connection
    symbol : str
    year : int
    best_value : float
        Value assigned to the *best* (lowest) trial.
    params : dict
        Params for the best trial.  A worse dummy trial (value = best + 0.5)
        is added first when *add_bad_trial* is True.
    add_bad_trial : bool
        Insert a dummy "worse" trial before the best one.
    """
    study_name = f"{symbol}_{year}_study"
    con.execute("INSERT INTO studies (study_name) VALUES (?)", (study_name,))
    study_id = con.execute(
        "SELECT study_id FROM studies WHERE study_name = ?", (study_name,)
    ).fetchone()[0]

    if add_bad_trial:
        # Worse trial (higher value)
        con.execute(
            "INSERT INTO trials (study_id, state, value) VALUES (?, 'COMPLETE', ?)",
            (study_id, best_value + 0.5),
        )

    # Best trial
    con.execute(
        "INSERT INTO trials (study_id, state, value) VALUES (?, 'COMPLETE', ?)",
        (study_id, best_value),
    )
    best_trial_id = con.execute(
        "SELECT trial_id FROM trials WHERE study_id = ? ORDER BY value ASC LIMIT 1",
        (study_id,),
    ).fetchone()[0]

    for param_name, param_value in params.items():
        # Choose distribution
        if param_name == "method":
            dist_json = _CATEGORICAL_DIST_METHOD
            # Store as index
            choices = ["moving_average", "vwap"]
            numeric_val = float(choices.index(param_value) if isinstance(param_value, str) else param_value)
        elif param_name == "allocation_strategy":
            dist_json = _CATEGORICAL_DIST_ALLOC
            choices = ["fixed_pct", "proportional", "volatility"]
            numeric_val = float(choices.index(param_value) if isinstance(param_value, str) else param_value)
        elif param_name in ("history_lookback", "period"):
            dist_json = _INT_DIST
            numeric_val = float(param_value)
        elif param_name in ("dynamic_midprice", "auto_set_grid"):
            dist_json = _CATEGORICAL_DIST_METHOD  # treat as categorical bool
            numeric_val = float(param_value)
        else:
            dist_json = _FLOAT_DIST
            numeric_val = float(param_value)

        con.execute(
            """
            INSERT INTO trial_params (trial_id, param_name, param_value, distribution_json)
            VALUES (?, ?, ?, ?)
            """,
            (best_trial_id, param_name, numeric_val, dist_json),
        )

    con.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """SQLite file pre-populated with STUDY_FIXTURES studies."""
    db = tmp_path / "optuna_study.db"
    with sqlite3.connect(str(db)) as con:
        _create_schema(con)
        for symbol, year, best_val, params in STUDY_FIXTURES:
            _insert_study(con, symbol, year, best_val, params)
    return db


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    """SQLite file with the schema but no studies."""
    db = tmp_path / "empty_optuna.db"
    with sqlite3.connect(str(db)) as con:
        _create_schema(con)
    return db


@pytest.fixture()
def shared_state(tmp_path: Path) -> SharedState:
    """Fresh SharedState backed by a temp JSON file."""
    return SharedState(persistence_path=str(tmp_path / "state.json"))


# ---------------------------------------------------------------------------
# Helper to build an optimizer that always uses the SQLite fallback
# (regardless of whether optuna is installed in the test environment)
# ---------------------------------------------------------------------------

def _make_fallback_optimizer(db_path: Path) -> OptunaGridOptimizer:
    """Return an OptunaGridOptimizer that is forced to use SQLiteOptunaFallback."""
    opt = OptunaGridOptimizer(db_path)
    # Patch _OPTUNA_AVAILABLE at module level so _list_studies / _read_study
    # take the fallback branch unconditionally.
    return opt


# ===========================================================================
# Tests
# ===========================================================================


class TestEmptyDb:
    def test_loads_empty_db(self, empty_db: Path) -> None:
        """Optimizer should load without error when the DB has no studies."""
        opt = OptunaGridOptimizer(empty_db)
        opt.load()
        assert opt.get_all_symbols() == []

    def test_get_best_config_returns_none_from_empty_db(self, empty_db: Path) -> None:
        opt = OptunaGridOptimizer(empty_db)
        opt.load()
        assert opt.get_best_config("TSLA") is None


class TestDbNotFound:
    def test_db_not_found_returns_empty(self, tmp_path: Path) -> None:
        """Missing DB file should log a warning and return empty results gracefully."""
        nonexistent = tmp_path / "no_such_file.db"
        opt = OptunaGridOptimizer(nonexistent)
        opt.load()  # must not raise
        assert opt.get_all_symbols() == []
        assert opt.get_best_config("TSLA") is None


class TestGetBestConfig:
    def test_get_best_config_returns_none_for_unknown_symbol(
        self, tmp_db: Path
    ) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        result = opt.get_best_config("MSFT")
        assert result is None

    def test_get_best_config_returns_gridconfigv2(self, tmp_db: Path) -> None:
        """A known symbol should return a valid GridConfig instance."""
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        cfg = opt.get_best_config("TSLA")
        assert cfg is not None
        assert isinstance(cfg, GridConfig)
        # TSLA 2023 has best_value = -0.85 (lower than 2022's -0.70)
        # so the 2023 params should be picked when no year is specified
        assert cfg.period == 14
        assert cfg.grid_distance == pytest.approx(0.004)

    def test_get_best_config_prefers_requested_year(self, tmp_db: Path) -> None:
        """When a specific year is given, that year's params should be returned."""
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        cfg = opt.get_best_config("TSLA", year=2022)
        assert cfg is not None
        # 2022 study has period=21
        assert cfg.period == 21

    def test_get_best_config_falls_back_to_best_value_when_year_missing(
        self, tmp_db: Path
    ) -> None:
        """When the requested year is absent, fall back to the best-value study."""
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        # TSLA 2024 does not exist → should fall back to 2023 (best_value=-0.85)
        cfg = opt.get_best_config("TSLA", year=2024)
        assert cfg is not None
        assert cfg.period == 14  # 2023 period

    def test_symbol_lookup_is_case_insensitive(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        cfg_upper = opt.get_best_config("TSLA")
        cfg_lower = opt.get_best_config("tsla")
        assert cfg_upper is not None
        assert cfg_lower is not None
        assert cfg_upper.grid_distance == cfg_lower.grid_distance

    def test_exchange_suffix_is_stripped(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        cfg = opt.get_best_config("TSLA.US")
        assert cfg is not None
        assert isinstance(cfg, GridConfig)


class TestGetAllSymbols:
    def test_get_all_symbols(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        symbols = opt.get_all_symbols()
        assert "TSLA" in symbols
        assert "AMZN" in symbols
        # Sorted, unique
        assert symbols == sorted(set(symbols))

    def test_get_all_symbols_empty(self, empty_db: Path) -> None:
        opt = OptunaGridOptimizer(empty_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        assert opt.get_all_symbols() == []


class TestGetStudySummary:
    def test_get_study_summary_has_correct_columns(self, tmp_db: Path) -> None:
        pytest.importorskip("pandas", reason="pandas required for this test")
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        df = opt.get_study_summary()
        expected_cols = {"symbol", "year", "best_value", "n_trials", "best_params"}
        assert expected_cols.issubset(set(df.columns))

    def test_get_study_summary_row_count(self, tmp_db: Path) -> None:
        pytest.importorskip("pandas", reason="pandas required for this test")
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        df = opt.get_study_summary()
        # STUDY_FIXTURES has 3 rows (TSLA_2023, TSLA_2022, AMZN_2023)
        assert len(df) == len(STUDY_FIXTURES)

    def test_get_study_summary_empty_db(self, empty_db: Path) -> None:
        pytest.importorskip("pandas", reason="pandas required for this test")
        opt = OptunaGridOptimizer(empty_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        df = opt.get_study_summary()
        assert len(df) == 0
        assert list(df.columns) == [
            "symbol",
            "year",
            "best_value",
            "n_trials",
            "best_params",
        ]


class TestInjectIntoSharedState:
    def test_inject_into_shared_state(
        self, tmp_db: Path, shared_state: SharedState
    ) -> None:
        """Injected entries should be retrievable from SharedState."""
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()

        module_id = "grid_trading_v1"
        opt.inject_into_shared_state(shared_state, module_id=module_id)

        # Each symbol should have an entry
        for symbol in opt.get_all_symbols():
            pair_key = f"optuna:{symbol}"
            entry = shared_state.get(module_id, pair_key)
            assert entry is not None, f"Missing entry for {symbol}"
            assert "data" in entry
            # The data should be a valid GridConfig dict
            cfg = GridConfig.from_dict(entry["data"])
            assert isinstance(cfg, GridConfig)

    def test_inject_does_not_raise_on_empty_db(
        self, empty_db: Path, shared_state: SharedState
    ) -> None:
        opt = OptunaGridOptimizer(empty_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        opt.inject_into_shared_state(shared_state, module_id="grid_trading_v1")
        # No entries injected but no crash
        assert len(shared_state) == 0

    def test_inject_produces_correct_grid_distance(
        self, tmp_db: Path, shared_state: SharedState
    ) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        opt.inject_into_shared_state(shared_state, module_id="grid_v2")

        # AMZN 2023 has grid_distance=0.003
        entry = shared_state.get("grid_v2", "optuna:AMZN")
        assert entry is not None
        assert entry["data"]["grid_distance"] == pytest.approx(0.003)


class TestSqliteFallback:
    def test_sqlite_fallback_when_optuna_missing(self, tmp_db: Path) -> None:
        """Force the SQLite fallback path and verify results match expectations."""
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt = OptunaGridOptimizer(tmp_db)
            opt.load()

        symbols = opt.get_all_symbols()
        assert "TSLA" in symbols
        assert "AMZN" in symbols

        cfg = opt.get_best_config("TSLA")
        assert cfg is not None
        assert isinstance(cfg, GridConfig)

    def test_sqlite_fallback_list_study_names(self, tmp_db: Path) -> None:
        fb = SQLiteOptunaFallback(tmp_db)
        names = fb.list_study_names()
        assert "TSLA_2023_study" in names
        assert "AMZN_2023_study" in names

    def test_sqlite_fallback_read_study_returns_record(self, tmp_db: Path) -> None:
        fb = SQLiteOptunaFallback(tmp_db)
        record = fb.read_study("TSLA_2023_study")
        assert record is not None
        assert record.symbol == "TSLA"
        assert record.year == 2023
        assert record.best_value == pytest.approx(-0.85)
        assert record.n_trials >= 2

    def test_sqlite_fallback_read_nonexistent_study(self, tmp_db: Path) -> None:
        fb = SQLiteOptunaFallback(tmp_db)
        record = fb.read_study("NONEXISTENT_2099_study")
        assert record is None

    def test_sqlite_fallback_on_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "no_db.db"
        fb = SQLiteOptunaFallback(missing)
        # Should not crash — returns empty list / None
        assert fb.list_study_names() == []
        assert fb.read_study("TSLA_2023_study") is None

    def test_sqlite_fallback_bad_study_name_skipped(self, tmp_db: Path) -> None:
        """Studies not matching the naming convention return None."""
        # Inject a badly-named study into the DB
        with sqlite3.connect(str(tmp_db)) as con:
            con.execute("INSERT INTO studies (study_name) VALUES ('bad_name')")
        fb = SQLiteOptunaFallback(tmp_db)
        record = fb.read_study("bad_name")
        assert record is None

    def test_sqlite_fallback_study_with_no_completed_trials(
        self, tmp_path: Path
    ) -> None:
        """A study with only RUNNING trials should return best_value=None."""
        db = tmp_path / "running.db"
        with sqlite3.connect(str(db)) as con:
            _create_schema(con)
            con.execute(
                "INSERT INTO studies (study_name) VALUES ('F_2023_study')"
            )
            study_id = con.execute(
                "SELECT study_id FROM studies WHERE study_name = 'F_2023_study'"
            ).fetchone()[0]
            con.execute(
                "INSERT INTO trials (study_id, state, value) VALUES (?, 'RUNNING', NULL)",
                (study_id,),
            )

        fb = SQLiteOptunaFallback(db)
        record = fb.read_study("F_2023_study")
        assert record is not None
        assert record.best_value is None
        assert record.best_params == {}


class TestLoadSymbolFilter:
    def test_load_symbol_filter(self, tmp_db: Path) -> None:
        """When symbols filter is given, only matching studies are cached."""
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load(symbols=["TSLA"])

        symbols = opt.get_all_symbols()
        assert "TSLA" in symbols
        assert "AMZN" not in symbols

    def test_load_symbol_filter_case_insensitive(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load(symbols=["amzn"])

        symbols = opt.get_all_symbols()
        assert "AMZN" in symbols
        assert "TSLA" not in symbols

    def test_load_symbol_filter_with_exchange_suffix(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load(symbols=["TSLA.US", "AMZN.US"])

        symbols = opt.get_all_symbols()
        assert "TSLA" in symbols
        assert "AMZN" in symbols


class TestStripExchangeSuffix:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("TSLA", "TSLA"),
            ("TSLA.US", "TSLA"),
            ("BRK.B", "BRK"),
            ("AMZN.NASDAQ", "AMZN"),
            ("", ""),
        ],
    )
    def test_strip_exchange_suffix(self, raw: str, expected: str) -> None:
        assert _strip_exchange_suffix(raw) == expected


class TestGridConfigRoundtrip:
    """Verify that GridConfig.from_dict / to_dict roundtrip correctly."""

    def test_roundtrip(self) -> None:
        original = GridConfig(
            grid_distance=0.005,
            grid_range=0.10,
            history_lookback=50,
            method="moving_average",
            period=14,
            dynamic_midprice=True,
            allocation_strategy="fixed_pct",
            allocation_param=1.0,
            volatility_threshold=0.03,
            deviation_threshold=0.02,
            auto_set_grid=False,
            selected_indicators=["ATR", "RSI"],
        )
        as_dict = original.to_dict()
        restored = GridConfig.from_dict(as_dict)
        assert restored.grid_distance == original.grid_distance
        assert restored.method == original.method
        assert restored.selected_indicators == original.selected_indicators

    def test_grid_count_derived(self) -> None:
        cfg = GridConfig(grid_distance=0.01, grid_range=0.10)
        assert cfg.grid_count() == 10

    def test_bounds_from_midprice(self) -> None:
        cfg = GridConfig(grid_distance=0.005, grid_range=10.0)
        mid = 100.0
        assert cfg.upper_bound(mid) == pytest.approx(105.0)
        assert cfg.lower_bound(mid) == pytest.approx(95.0)

    def test_invalid_method_raises(self) -> None:
        with pytest.raises(ValueError, match="method"):
            GridConfig(method="zigzag")  # type: ignore[arg-type]

    def test_invalid_allocation_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="allocation_strategy"):
            GridConfig(allocation_strategy="random")  # type: ignore[arg-type]

    def test_invalid_indicator_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown indicator"):
            GridConfig(selected_indicators=["ATR", "MACD_v99"])

    def test_from_dict_ignores_unknown_keys(self) -> None:
        d = {
            "grid_distance": 0.005,
            "grid_range": 0.10,
            "method": "moving_average",
            "unknown_future_param": "ignored",
        }
        cfg = GridConfig.from_dict(d)
        assert cfg.grid_distance == pytest.approx(0.005)


class TestOptimizerRepr:
    def test_repr_contains_path(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        assert str(tmp_db) in repr(opt)

    def test_repr_after_load(self, tmp_db: Path) -> None:
        opt = OptunaGridOptimizer(tmp_db)
        with patch(
            "algo_system.orchestrator.optuna_grid_optimizer._OPTUNA_AVAILABLE", False
        ):
            opt.load()
        r = repr(opt)
        assert "loaded=True" in r
        assert "TSLA" in r

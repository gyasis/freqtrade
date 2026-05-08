"""
orchestrator/optuna_grid_optimizer.py
======================================
OptunaGridOptimizer — loads pre-computed Optuna study results from a SQLite
database and maps them to ``GridConfig`` objects that ``GridTradingModule``
can consume at initialisation time.

This module is a **pure utility** — it has no freqtrade imports and does not
implement ``IAlgoModule``.  It is intended to be instantiated once (e.g. from
the strategy's ``__init__``) and queried via ``inject_into_shared_state`` so
that ``GridTradingModule.initialize()`` can pull per-symbol tuned configs from
``SharedState``.

Optuna availability
-------------------
The ``optuna`` package is imported inside a ``try/except ImportError`` block.
If Optuna is not installed, ``SQLiteOptunaFallback`` is used instead — it
reads the same SQLite database directly via the ``sqlite3`` standard library
module.  The public API is identical in both cases.

Study naming convention
-----------------------
Studies are expected to be named ``{SYMBOL}_{YEAR}_study``, for example
``"TSLA_2023_study"`` or ``"AMZN_2024_study"``.  The optimizer also handles
exchange-suffixed symbols: ``"TSLA.US"`` is normalised to ``"TSLA"`` before
lookup.

Usage example
-------------
::

    from algo_system.orchestrator.optuna_grid_optimizer import OptunaGridOptimizer

    optimizer = OptunaGridOptimizer("user_data/optuna_study.db")
    optimizer.load()

    cfg = optimizer.get_best_config("TSLA", year=2023)
    if cfg is not None:
        print(cfg)

    optimizer.inject_into_shared_state(shared_state, module_id="grid_trading_v1")
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PANDAS_AVAILABLE = False

try:
    import optuna as optuna  # noqa: PLC0414
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None  # type: ignore[assignment]
    _OPTUNA_AVAILABLE = False

from ..config.grid_config import GridConfig

logger = logging.getLogger("algo_system.optuna_grid_optimizer")

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

# Regex that matches the study naming convention: SYMBOL_YEAR_study
_STUDY_NAME_RE = re.compile(r"^(?P<symbol>[A-Z0-9]+)_(?P<year>\d{4})_study$")


@dataclass
class _StudyRecord:
    """Internal container for one loaded study."""

    study_name: str
    symbol: str
    year: int
    best_value: Optional[float]
    n_trials: int
    best_params: Dict[str, Any]


# ---------------------------------------------------------------------------
# SQLite fallback (no optuna dependency)
# ---------------------------------------------------------------------------


class SQLiteOptunaFallback:
    """Read Optuna study / trial data directly from the SQLite database.

    This class reimplements the minimal subset of the Optuna storage layer
    needed by ``OptunaGridOptimizer`` so that the optimizer works even when
    the ``optuna`` package is not installed.

    The Optuna SQLite schema (as of optuna >= 3.x) stores:

    - ``studies`` table: ``study_id``, ``study_name``
    - ``trials`` table: ``trial_id``, ``study_id``, ``state``, ``value``
    - ``trial_params`` table: ``trial_id``, ``param_name``, ``param_value``,
      ``distribution_json``

    All reads are done with a fresh ``sqlite3.connect()`` per call so the
    class is stateless and thread-safe.

    Parameters
    ----------
    db_path : Path
        Absolute path to the Optuna SQLite file.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def list_study_names(self) -> List[str]:
        """Return all study names present in the database.

        Returns
        -------
        list[str]
            Study names; empty list if the database is empty or missing the
            expected schema.
        """
        try:
            with sqlite3.connect(str(self._db_path)) as con:
                cur = con.execute("SELECT study_name FROM studies ORDER BY study_id")
                return [row[0] for row in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.warning(
                "SQLiteOptunaFallback.list_study_names: error reading %s: %s",
                self._db_path,
                exc,
            )
            return []

    def read_study(self, study_name: str) -> Optional[_StudyRecord]:
        """Return a ``_StudyRecord`` for *study_name*, or ``None`` if not found.

        The *best* trial is the completed trial with the minimum ``value``
        (Optuna minimises by default; if the study was created to maximise the
        best trial still has ``direction='maximize'`` — we simply pick the
        trial with the lowest stored value, which Optuna also stores as the
        best trial).

        Parameters
        ----------
        study_name : str
            Exact study name string.

        Returns
        -------
        _StudyRecord or None
        """
        m = _STUDY_NAME_RE.match(study_name)
        if not m:
            logger.debug(
                "SQLiteOptunaFallback.read_study: study %r does not match naming convention",
                study_name,
            )
            return None

        symbol = m.group("symbol")
        year = int(m.group("year"))

        try:
            with sqlite3.connect(str(self._db_path)) as con:
                con.row_factory = sqlite3.Row

                # Fetch study id
                row = con.execute(
                    "SELECT study_id FROM studies WHERE study_name = ?",
                    (study_name,),
                ).fetchone()
                if row is None:
                    return None
                study_id = row["study_id"]

                # Count trials
                n_trials: int = con.execute(
                    "SELECT COUNT(*) FROM trials WHERE study_id = ?",
                    (study_id,),
                ).fetchone()[0]

                # Find best completed trial (min value — default minimize direction)
                best_trial_row = con.execute(
                    """
                    SELECT trial_id, value
                    FROM trials
                    WHERE study_id = ?
                      AND state = 'COMPLETE'
                      AND value IS NOT NULL
                    ORDER BY value ASC
                    LIMIT 1
                    """,
                    (study_id,),
                ).fetchone()

                if best_trial_row is None:
                    # No completed trials — return a stub record
                    return _StudyRecord(
                        study_name=study_name,
                        symbol=symbol,
                        year=year,
                        best_value=None,
                        n_trials=n_trials,
                        best_params={},
                    )

                best_trial_id = best_trial_row["trial_id"]
                best_value: float = best_trial_row["value"]

                # Fetch params for that trial
                param_rows = con.execute(
                    """
                    SELECT param_name, param_value, distribution_json
                    FROM trial_params
                    WHERE trial_id = ?
                    """,
                    (best_trial_id,),
                ).fetchall()

                best_params: Dict[str, Any] = {}
                for p in param_rows:
                    best_params[p["param_name"]] = self._coerce_param(
                        p["param_value"], p["distribution_json"]
                    )

                return _StudyRecord(
                    study_name=study_name,
                    symbol=symbol,
                    year=year,
                    best_value=best_value,
                    n_trials=n_trials,
                    best_params=best_params,
                )

        except sqlite3.Error as exc:
            logger.warning(
                "SQLiteOptunaFallback.read_study: error reading study %r: %s",
                study_name,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_param(raw_value: Any, distribution_json: Optional[str]) -> Any:
        """Coerce a raw SQLite param value to the correct Python type.

        Optuna stores all param values as floats in ``trial_params.param_value``.
        The ``distribution_json`` column records the distribution type which
        tells us whether to cast back to int or bool.

        Parameters
        ----------
        raw_value : Any
            Raw value from the ``param_value`` column (usually a float).
        distribution_json : str or None
            JSON string describing the Optuna distribution.

        Returns
        -------
        Any
            The value cast to the appropriate Python type.
        """
        if distribution_json is None:
            return raw_value

        try:
            dist = json.loads(distribution_json)
        except (json.JSONDecodeError, TypeError):
            return raw_value

        name: str = dist.get("name", "")

        if name in ("IntDistribution", "IntUniformDistribution", "IntLogUniformDistribution"):
            return int(round(float(raw_value)))

        if name in ("CategoricalDistribution",):
            # Categorical: raw_value is the index into the choices list
            choices = dist.get("attributes", {}).get("choices", [])
            if not choices:
                # Newer Optuna versions store choices directly
                choices = dist.get("choices", [])
            idx = int(round(float(raw_value)))
            if 0 <= idx < len(choices):
                return choices[idx]
            return raw_value

        # FloatDistribution, UniformDistribution, LogUniformDistribution → float
        return float(raw_value)


# ---------------------------------------------------------------------------
# OptunaGridOptimizer
# ---------------------------------------------------------------------------


class OptunaGridOptimizer:
    """Load Optuna study results and map them to ``GridConfig`` configs.

    The optimizer maintains an internal cache of ``_StudyRecord`` objects
    (populated by :py:meth:`load`) keyed by ``(symbol_upper, year)``.

    Parameters
    ----------
    db_path : str or Path
        Path to the ``optuna_study.db`` SQLite file produced by Optuna.
        The file need not exist at construction time — missing files are
        handled gracefully in :py:meth:`load`.
    """

    def __init__(self, db_path: "str | Path") -> None:
        self._db_path: Path = Path(db_path).expanduser().resolve()
        # Cache: (SYMBOL_UPPER, year) → _StudyRecord
        self._cache: Dict[Tuple[str, int], _StudyRecord] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, symbols: "Optional[List[str]]" = None) -> None:
        """Read all (or a filtered subset of) studies from the SQLite DB.

        Populates the internal cache.  Safe to call multiple times — each
        call replaces the previous cache contents.

        Parameters
        ----------
        symbols : list[str] or None
            When given, only studies whose symbol matches an entry in this
            list (case-insensitive, exchange suffix stripped) are loaded.
            When ``None`` all studies are loaded.
        """
        self._cache.clear()
        self._loaded = False

        if not self._db_path.exists():
            logger.warning(
                "OptunaGridOptimizer.load: database not found at %s — "
                "returning empty results",
                self._db_path,
            )
            self._loaded = True
            return

        # Normalise filter list once
        symbol_filter: Optional[frozenset[str]] = None
        if symbols is not None:
            symbol_filter = frozenset(
                _strip_exchange_suffix(s).upper() for s in symbols
            )

        study_names = self._list_studies()
        loaded_count = 0

        for name in study_names:
            m = _STUDY_NAME_RE.match(name)
            if not m:
                logger.debug(
                    "OptunaGridOptimizer.load: skipping study %r (naming mismatch)",
                    name,
                )
                continue

            symbol = m.group("symbol").upper()
            year = int(m.group("year"))

            if symbol_filter is not None and symbol not in symbol_filter:
                continue

            record = self._read_study(name)
            if record is not None:
                self._cache[(symbol, year)] = record
                loaded_count += 1
                logger.debug(
                    "Loaded study %r: symbol=%s year=%d best_value=%s n_trials=%d",
                    name,
                    symbol,
                    year,
                    record.best_value,
                    record.n_trials,
                )

        self._loaded = True
        logger.info(
            "OptunaGridOptimizer.load: loaded %d studies from %s",
            loaded_count,
            self._db_path,
        )

    def get_best_config(
        self,
        symbol: str,
        year: Optional[int] = None,
    ) -> Optional[GridConfig]:
        """Return the ``GridConfig`` for *symbol* tuned by Optuna.

        Parameters
        ----------
        symbol : str
            Trading symbol, e.g. ``"TSLA"`` or ``"TSLA.US"``.  Exchange
            suffixes are stripped automatically.
        year : int or None
            When provided, prefer the study for that exact year.  If no study
            exists for that year the method falls back to the study with the
            best objective value across all years for this symbol.  When
            ``None``, the study with the best objective value is used.

        Returns
        -------
        GridConfig or None
            A fully validated ``GridConfig`` built from the best trial's
            params, or ``None`` if no data exists for *symbol*.
        """
        if not self._loaded:
            logger.warning(
                "OptunaGridOptimizer.get_best_config: call load() before get_best_config()"
            )

        norm_symbol = _strip_exchange_suffix(symbol).upper()

        # Gather all records for this symbol
        candidates: List[_StudyRecord] = [
            rec
            for (sym, _yr), rec in self._cache.items()
            if sym == norm_symbol
        ]

        if not candidates:
            return None

        # Prefer exact year match
        if year is not None:
            exact = self._cache.get((norm_symbol, year))
            if exact is not None and exact.best_params:
                return _params_to_config(exact.best_params)
            # Fall through to best-value selection when no exact match

        # Pick the record with the best (lowest) objective value
        candidates_with_value = [c for c in candidates if c.best_value is not None]
        if candidates_with_value:
            best_record = min(candidates_with_value, key=lambda r: r.best_value)  # type: ignore[arg-type]
        elif candidates:
            best_record = candidates[0]
        else:
            return None

        if not best_record.best_params:
            return None

        return _params_to_config(best_record.best_params)

    def get_all_symbols(self) -> List[str]:
        """Return the distinct symbols for which study data exists.

        Returns
        -------
        list[str]
            Sorted list of uppercase symbol strings.
        """
        return sorted({sym for sym, _yr in self._cache})

    def get_study_summary(self) -> "Any":  # returns pd.DataFrame when pandas available
        """Return a summary table of all loaded studies.

        Returns
        -------
        pandas.DataFrame
            Columns: ``symbol``, ``year``, ``best_value``, ``n_trials``,
            ``best_params`` (dict).
            Returns an empty ``DataFrame`` when no studies are loaded.

        Notes
        -----
        This method requires ``pandas``.  If pandas is not installed an
        ``ImportError`` is raised with a descriptive message.
        """
        if not _PANDAS_AVAILABLE:
            raise ImportError(
                "pandas is required for get_study_summary(). "
                "Install it with: pip install pandas"
            )

        import pandas as pd  # noqa: PLC0415

        rows = [
            {
                "symbol": rec.symbol,
                "year": rec.year,
                "best_value": rec.best_value,
                "n_trials": rec.n_trials,
                "best_params": rec.best_params,
            }
            for rec in self._cache.values()
        ]

        if not rows:
            return pd.DataFrame(
                columns=["symbol", "year", "best_value", "n_trials", "best_params"]
            )

        df = pd.DataFrame(rows)
        return df.sort_values(["symbol", "year"]).reset_index(drop=True)

    def inject_into_shared_state(
        self,
        shared_state: Any,
        module_id: str,
    ) -> None:
        """Write each symbol's best config into *shared_state*.

        Each symbol's best ``GridConfig`` (across all years, by objective
        value) is serialised via ``GridConfig.to_dict()`` and written to
        ``shared_state`` under key ``(module_id, "optuna:{symbol}")``.

        ``GridTradingModule.initialize()`` can then retrieve these at startup:

        .. code-block:: python

            entry = ctx.shared_state.get(self.module_id, f"optuna:{symbol}")
            if entry:
                cfg = GridConfig.from_dict(entry["data"])

        Parameters
        ----------
        shared_state : SharedState
            The LATS ``SharedState`` instance owned by the orchestrator.
        module_id : str
            The module_id key used when writing to shared_state
            (typically ``GridTradingModule.module_id``).
        """
        if not self._loaded:
            logger.warning(
                "OptunaGridOptimizer.inject_into_shared_state: "
                "call load() before injecting"
            )

        injected = 0
        for symbol in self.get_all_symbols():
            cfg = self.get_best_config(symbol)
            if cfg is None:
                continue
            pair_key = f"optuna:{symbol}"
            shared_state.set(module_id, pair_key, cfg.to_dict())
            logger.debug(
                "Injected optuna config for symbol=%s into shared_state[%r][%r]",
                symbol,
                module_id,
                pair_key,
            )
            injected += 1

        logger.info(
            "OptunaGridOptimizer.inject_into_shared_state: "
            "injected configs for %d symbols (module_id=%r)",
            injected,
            module_id,
        )

    # ------------------------------------------------------------------
    # Introspection / debugging
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"OptunaGridOptimizer("
            f"db_path={str(self._db_path)!r}, "
            f"loaded={self._loaded}, "
            f"n_studies={len(self._cache)}, "
            f"symbols={self.get_all_symbols()}"
            f")"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_studies(self) -> List[str]:
        """Return all study names in the DB using optuna or the fallback."""
        if _OPTUNA_AVAILABLE:
            try:
                storage_url = f"sqlite:///{self._db_path}"
                summaries = optuna.get_all_study_summaries(storage=storage_url)
                return [s.study_name for s in summaries]
            except Exception as exc:
                logger.warning(
                    "_list_studies: optuna raised %s; falling back to SQLite", exc
                )

        fb = SQLiteOptunaFallback(self._db_path)
        return fb.list_study_names()

    def _read_study(self, study_name: str) -> Optional[_StudyRecord]:
        """Load a single study by name using optuna or the fallback."""
        if _OPTUNA_AVAILABLE:
            try:
                return self._read_study_optuna(study_name)
            except Exception as exc:
                logger.warning(
                    "_read_study: optuna raised %s for %r; falling back to SQLite",
                    exc,
                    study_name,
                )

        fb = SQLiteOptunaFallback(self._db_path)
        return fb.read_study(study_name)

    def _read_study_optuna(self, study_name: str) -> Optional[_StudyRecord]:
        """Load a study using the optuna Python API.

        Parameters
        ----------
        study_name : str
            Study name to load.

        Returns
        -------
        _StudyRecord or None
        """
        m = _STUDY_NAME_RE.match(study_name)
        if not m:
            return None

        symbol = m.group("symbol").upper()
        year = int(m.group("year"))

        storage_url = f"sqlite:///{self._db_path}"
        study = optuna.load_study(study_name=study_name, storage=storage_url)

        n_trials = len(study.trials)

        try:
            best_trial = study.best_trial
            best_value: Optional[float] = best_trial.value
            best_params: Dict[str, Any] = dict(best_trial.params)
        except ValueError:
            # No completed trials
            best_value = None
            best_params = {}

        return _StudyRecord(
            study_name=study_name,
            symbol=symbol,
            year=year,
            best_value=best_value,
            n_trials=n_trials,
            best_params=best_params,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _strip_exchange_suffix(symbol: str) -> str:
    """Strip a dotted exchange suffix from a symbol string.

    Examples
    --------
    >>> _strip_exchange_suffix("TSLA.US")
    'TSLA'
    >>> _strip_exchange_suffix("AMZN")
    'AMZN'
    >>> _strip_exchange_suffix("BRK.B")
    'BRK'

    Parameters
    ----------
    symbol : str
        Raw symbol string, possibly with an exchange suffix.

    Returns
    -------
    str
        Symbol with everything from the first ``.`` onward removed.
    """
    return symbol.split(".")[0]


# Canonical mapping from Optuna param names → GridConfig field names.
# When an Optuna param name differs from the dataclass field name, add an
# entry here.  Identity mappings (name == field) need not be listed — they
# are handled by the ``from_dict`` fallthrough.
_PARAM_ALIASES: Dict[str, str] = {
    # Example aliases for non-standard param names emitted by the study:
    # "grid_dist": "grid_distance",
    # "lookback": "history_lookback",
}


def _params_to_config(params: Dict[str, Any]) -> Optional[GridConfig]:
    """Convert an Optuna best-params dict to a ``GridConfig`` instance.

    Parameters
    ----------
    params : dict
        Raw ``trial.params`` dict from Optuna (or the SQLite fallback).

    Returns
    -------
    GridConfig or None
        ``None`` if ``GridConfig.__post_init__`` raises a ``ValueError``
        (e.g. the DB contains params from an old study schema that is no
        longer valid).
    """
    # Apply aliases first
    normalised: Dict[str, Any] = {}
    for k, v in params.items():
        field_name = _PARAM_ALIASES.get(k, k)
        normalised[field_name] = v

    # Coerce selected_indicators: Optuna stores it as a comma-separated string
    # if the study used CategoricalDistribution with a single combined value,
    # or as individual boolean flags like "use_ATR", "use_RSI".
    normalised = _coerce_indicators(normalised)

    try:
        return GridConfig.from_dict(normalised)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "_params_to_config: could not build GridConfig from params %r: %s",
            params,
            exc,
        )
        return None


def _coerce_indicators(params: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the ``selected_indicators`` list from raw Optuna params.

    The study may encode indicators in one of three ways:

    1. **Direct list** — ``params["selected_indicators"] = ["ATR", "RSI"]``
    2. **Comma-separated string** — ``params["selected_indicators"] = "ATR,RSI"``
    3. **Boolean flags** — ``params["use_ATR"] = True``, ``params["use_RSI"] = False``

    This function normalises all three representations into a plain
    ``list[str]`` stored under ``"selected_indicators"``.

    Unknown flag names (not in ``VALID_INDICATORS``) are ignored.

    Parameters
    ----------
    params : dict
        Possibly-modified params dict (aliases already applied).

    Returns
    -------
    dict
        Params dict with ``"selected_indicators"`` in canonical list form.
    """
    from ..config.grid_config import VALID_INDICATORS  # noqa: PLC0415

    result = dict(params)

    if "selected_indicators" in result:
        raw = result["selected_indicators"]
        if isinstance(raw, str):
            # Comma-separated or single indicator name
            indicators = [s.strip() for s in raw.split(",") if s.strip()]
            result["selected_indicators"] = [i for i in indicators if i in VALID_INDICATORS]
        elif isinstance(raw, (list, tuple)):
            result["selected_indicators"] = [
                str(i) for i in raw if str(i) in VALID_INDICATORS
            ]
        # else: leave as-is and let GridConfig.__post_init__ validate
        return result

    # Boolean flag pattern: "use_ATR", "use_RSI", etc.
    selected: List[str] = []
    flag_keys_seen: List[str] = []
    for indicator in VALID_INDICATORS:
        flag_key = f"use_{indicator}"
        if flag_key in result:
            flag_keys_seen.append(flag_key)
            if result[flag_key]:
                selected.append(indicator)

    if flag_keys_seen:
        # Remove the individual flag keys and replace with the consolidated list
        for k in flag_keys_seen:
            result.pop(k, None)
        result["selected_indicators"] = selected

    return result

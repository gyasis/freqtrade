"""
orchestrator/screener_weights_optimizer.py
==========================================
ScreenerWeightsOptimizer — uses Optuna's Bayesian TPE sampler to find the
optimal indicator weights for StockScreenerModule.

Workflow
--------
  opt = ScreenerWeightsOptimizer(db_path="screener_weights.db", api_key="...")
  opt.run(symbols=[...], date_from="2022-01-01", date_to="2023-12-31", n_trials=100)
  best = opt.get_best_weights()          # -> ScoringWeights
  opt.inject_into_shared_state(shared_state, "stock_screener")

The optimiser uses the ScreenerGym as its objective function.
StockScreenerModule reads the injected weights from SharedState at startup.

Optuna availability
-------------------
The ``optuna`` package is imported inside a ``try/except ImportError`` block.
If Optuna is not installed, ``SQLiteWeightsFallback`` is used to read best
weights from the SQLite database directly via the ``sqlite3`` stdlib module.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .screener_gym import ScreenerGym, ScoringWeights, WEIGHT_FIELDS

log = logging.getLogger(__name__)

try:
    import optuna as optuna  # noqa: PLC0414
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _OPTUNA_AVAILABLE = True
except ImportError:
    optuna = None  # type: ignore[assignment]
    _OPTUNA_AVAILABLE = False


# ---------------------------------------------------------------------------
# SQLite fallback (no optuna dependency)
# ---------------------------------------------------------------------------


class SQLiteWeightsFallback:
    """Read best ScoringWeights from an Optuna SQLite database without the optuna package.

    The Optuna SQLite schema (as of optuna >= 3.x) stores:
      - ``studies`` table:     study_id, study_name
      - ``trials`` table:      trial_id, study_id, state, value
      - ``trial_params`` table: trial_id, param_name, param_value, distribution_json

    For a ``direction='maximize'`` study the best trial has the *highest*
    stored value.  This class selects the COMPLETE trial with the maximum value
    and reads back its weight params.

    Parameters
    ----------
    db_path : Path
        Absolute path to the Optuna SQLite file.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get_best_weights(self, study_name: str) -> Optional[ScoringWeights]:
        """Return the best ``ScoringWeights`` found in *study_name*, or ``None``.

        Returns ``None`` when:
          - The database file does not exist.
          - The study does not exist in the database.
          - No COMPLETE trials exist for the study.
          - The trial params do not contain any recognised weight field.

        Parameters
        ----------
        study_name : str
            Exact name of the Optuna study to look up.

        Returns
        -------
        ScoringWeights or None
        """
        if not self._db_path.exists():
            log.debug("SQLiteWeightsFallback: db not found at %s", self._db_path)
            return None

        try:
            with sqlite3.connect(str(self._db_path)) as con:
                con.row_factory = sqlite3.Row

                # Look up study_id
                row = con.execute(
                    "SELECT study_id FROM studies WHERE study_name = ?",
                    (study_name,),
                ).fetchone()
                if row is None:
                    log.debug(
                        "SQLiteWeightsFallback: study %r not found in %s",
                        study_name,
                        self._db_path,
                    )
                    return None
                study_id = row["study_id"]

                # Find best COMPLETE trial (maximize -> highest value)
                best_row = con.execute(
                    """
                    SELECT trial_id, value
                    FROM trials
                    WHERE study_id = ?
                      AND state = 'COMPLETE'
                      AND value IS NOT NULL
                    ORDER BY value DESC
                    LIMIT 1
                    """,
                    (study_id,),
                ).fetchone()

                if best_row is None:
                    log.debug(
                        "SQLiteWeightsFallback: no complete trials in study %r",
                        study_name,
                    )
                    return None

                best_trial_id = best_row["trial_id"]

                # Fetch all params for that trial
                param_rows = con.execute(
                    """
                    SELECT param_name, param_value, distribution_json
                    FROM trial_params
                    WHERE trial_id = ?
                    """,
                    (best_trial_id,),
                ).fetchall()

                params: Dict[str, float] = {}
                for p in param_rows:
                    name = p["param_name"]
                    if name in WEIGHT_FIELDS:
                        params[name] = self._coerce_float(
                            p["param_value"], p["distribution_json"]
                        )

                if not params:
                    return None

                return ScoringWeights.from_dict(params)

        except sqlite3.Error as exc:
            log.warning(
                "SQLiteWeightsFallback.get_best_weights: sqlite error: %s", exc
            )
            return None

    @staticmethod
    def _coerce_float(raw_value: Any, distribution_json: Optional[str]) -> float:
        """Coerce a raw SQLite param value to float.

        Optuna stores all float-distribution values as floats in
        ``trial_params.param_value`` so coercion is usually a no-op.
        The distribution_json is checked to validate the expected type.
        """
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return 0.0


# ---------------------------------------------------------------------------
# ScreenerWeightsOptimizer
# ---------------------------------------------------------------------------


class ScreenerWeightsOptimizer:
    """Run Bayesian weight optimisation for StockScreenerModule via Optuna TPE.

    Parameters
    ----------
    db_path : str or Path
        Path to the SQLite file used as Optuna storage.  Created automatically
        when :py:meth:`run` is called if it does not exist.
    api_key : str
        EODHD API key passed to ScreenerGym for history loading.
    """

    def __init__(self, db_path: "str | Path", api_key: str) -> None:
        self._db_path: Path = Path(db_path).expanduser().resolve()
        self._api_key = api_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbols: List[str],
        date_from: str,
        date_to: str,
        n_trials: int = 100,
        top_n: int = 10,
        hold_days: int = 5,
        study_name: str = "screener_weights",
    ) -> ScoringWeights:
        """Run Bayesian optimisation and return the best ScoringWeights found.

        Parameters
        ----------
        symbols : list[str]
            Ticker symbols (without .US suffix) for the backtest universe.
        date_from : str
            Start date of the backtest window ("YYYY-MM-DD").
        date_to : str
            End date of the backtest window ("YYYY-MM-DD").
        n_trials : int
            Number of Optuna trials to run.
        top_n : int
            Number of top-ranked symbols selected per evaluation date.
        hold_days : int
            Forward-return horizon passed to ``ScreenerGym.evaluate``.
        study_name : str
            Optuna study name (used as a key in the SQLite storage).

        Returns
        -------
        ScoringWeights
            Weights from the best trial found.

        Raises
        ------
        ImportError
            If ``optuna`` is not installed and no fallback data exists.
        """
        if not _OPTUNA_AVAILABLE:
            raise ImportError(
                "optuna is required to run weight optimisation. "
                "Install it with: pip install optuna"
            )

        gym = ScreenerGym(api_key=self._api_key)
        log.info(
            "ScreenerWeightsOptimizer: loading history for %d symbols (%s to %s)",
            len(symbols),
            date_from,
            date_to,
        )
        gym.load_history(symbols=symbols, date_from=date_from, date_to=date_to)
        log.info("ScreenerWeightsOptimizer: gym summary: %s", gym.summary())

        storage_url = f"sqlite:///{self._db_path}"
        study = optuna.create_study(  # type: ignore[union-attr]
            study_name=study_name,
            direction="maximize",
            storage=storage_url,
            load_if_exists=True,
        )

        def objective(trial: Any) -> float:
            weights = ScoringWeights.from_optuna_trial(trial)
            return gym.evaluate(weights=weights, top_n=top_n, hold_days=hold_days)

        log.info(
            "ScreenerWeightsOptimizer: starting optimisation (n_trials=%d)", n_trials
        )
        study.optimize(objective, n_trials=n_trials)

        best = ScoringWeights.from_dict(study.best_params)
        log.info(
            "ScreenerWeightsOptimizer: best value=%.6f, weights=%s",
            study.best_value,
            best.to_dict(),
        )
        return best

    def get_best_weights(
        self, study_name: str = "screener_weights"
    ) -> Optional[ScoringWeights]:
        """Read the best weights from the SQLite DB without re-running optimisation.

        Uses the Optuna Python API when available; falls back to
        ``SQLiteWeightsFallback`` otherwise.

        Parameters
        ----------
        study_name : str
            Study name to look up.

        Returns
        -------
        ScoringWeights or None
            ``None`` when the database does not exist or the study has no
            completed trials.
        """
        if not self._db_path.exists():
            log.debug(
                "ScreenerWeightsOptimizer.get_best_weights: db not found at %s",
                self._db_path,
            )
            return None

        if _OPTUNA_AVAILABLE:
            try:
                storage_url = f"sqlite:///{self._db_path}"
                study = optuna.load_study(  # type: ignore[union-attr]
                    study_name=study_name, storage=storage_url
                )
                return ScoringWeights.from_dict(study.best_params)
            except Exception as exc:
                log.warning(
                    "get_best_weights: optuna raised %s; falling back to SQLite", exc
                )

        fb = SQLiteWeightsFallback(self._db_path)
        return fb.get_best_weights(study_name)

    def inject_into_shared_state(
        self,
        shared_state: Any,
        module_id: str = "stock_screener",
    ) -> None:
        """Write the best ScoringWeights into *shared_state* for StockScreenerModule.

        StockScreenerModule.initialize() reads this entry and uses the weights
        dict to override the hard-coded scoring values in ``_score_and_rank``.

        The entry is written under ``(module_id, "scoring_weights")`` so that:

        .. code-block:: python

            entry = context.shared_state.get(module_id, "scoring_weights")
            # entry["data"] is a dict[str, float] of weight values

        Parameters
        ----------
        shared_state : SharedState
            The LATS SharedState instance.
        module_id : str
            Module id key (matches ``StockScreenerModule.module_id``).
        """
        weights = self.get_best_weights()
        if weights is None:
            log.warning(
                "inject_into_shared_state: no best weights found — "
                "run ScreenerWeightsOptimizer.run() first or provide a seeded DB"
            )
            return

        shared_state.set(module_id, "scoring_weights", weights.to_dict())
        log.info(
            "ScreenerWeightsOptimizer: injected scoring weights into "
            "shared_state[%r]['scoring_weights']",
            module_id,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"ScreenerWeightsOptimizer("
            f"db_path={str(self._db_path)!r}, "
            f"optuna_available={_OPTUNA_AVAILABLE})"
        )

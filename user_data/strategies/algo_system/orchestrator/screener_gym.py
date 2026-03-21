"""
orchestrator/screener_gym.py
============================
ScreenerGym — backtests screener indicator-weight configurations against
historical EODHD data to produce an objective score (mean top-N 5-day return).

Used by ScreenerWeightsOptimizer to find optimal indicator weights via Optuna's
Bayesian (TPE) sampler.

Architecture
------------
  ScreenerGym.load_history(symbols, date_from, date_to)
      -> fetches EOD OHLCV + indicators for each symbol via EODHD APIClient
      -> stores as self._history: Dict[str, pd.DataFrame]

  ScreenerGym.evaluate(weights: ScoringWeights, top_n, hold_days) -> float
      -> for each evaluation date in the history window:
          1. score every symbol using weights on that date's indicator values
          2. take the top_n symbols by score
          3. measure hold_days forward return for each selected symbol
      -> objective = mean forward return across all dates and selections
      -> higher is better (maximise)

ScoringWeights dataclass
------------------------
  w_bull_market    float   weight for bull_market == True
  w_next_action    float   weight for next_action == "buy"
  w_ema_cross      float   weight for ema12 > ema26
  w_above_sma50    float   weight for price > sma50
  w_above_sma200   float   weight for price > sma200
  w_atr_volatile   float   weight for atr14_pcnt > 1.0
  w_adx_strong     float   weight for adx > 25
  w_adx_very_strong float  weight for adx > 40 (additional)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path  # noqa: F401  (re-exported for consumers)

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

WEIGHT_FIELDS = [
    "w_bull_market",
    "w_next_action",
    "w_ema_cross",
    "w_above_sma50",
    "w_above_sma200",
    "w_atr_volatile",
    "w_adx_strong",
    "w_adx_very_strong",
]


@dataclass
class ScoringWeights:
    """Indicator scoring weights used by ScreenerGym and StockScreenerModule.

    Default values mirror the hard-coded defaults in StockScreenerModule so
    that an un-optimised system behaves identically to the original code.
    """

    w_bull_market: float = 2.0
    w_next_action: float = 1.0
    w_ema_cross: float = 1.0
    w_above_sma50: float = 0.5
    w_above_sma200: float = 0.5
    w_atr_volatile: float = 1.0
    w_adx_strong: float = 1.5
    w_adx_very_strong: float = 0.5

    def to_dict(self) -> Dict[str, float]:
        """Serialise to a plain dict suitable for SharedState storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScoringWeights":
        """Deserialise from a dict, ignoring unknown keys gracefully."""
        return cls(**{k: float(v) for k, v in d.items() if k in WEIGHT_FIELDS})

    @classmethod
    def from_optuna_trial(cls, trial: Any) -> "ScoringWeights":
        """Create ScoringWeights from an Optuna trial's suggest_float calls."""
        return cls(
            w_bull_market=trial.suggest_float("w_bull_market", 0.0, 3.0),
            w_next_action=trial.suggest_float("w_next_action", 0.0, 2.0),
            w_ema_cross=trial.suggest_float("w_ema_cross", 0.0, 2.0),
            w_above_sma50=trial.suggest_float("w_above_sma50", 0.0, 1.5),
            w_above_sma200=trial.suggest_float("w_above_sma200", 0.0, 1.5),
            w_atr_volatile=trial.suggest_float("w_atr_volatile", 0.0, 2.0),
            w_adx_strong=trial.suggest_float("w_adx_strong", 0.0, 2.0),
            w_adx_very_strong=trial.suggest_float("w_adx_very_strong", 0.0, 1.0),
        )


class ScreenerGym:
    """
    Backtesting harness for screener indicator weights.

    Usage
    -----
    gym = ScreenerGym(api_key="YOUR_KEY")
    gym.load_history(symbols=["AAPL", "TSLA", ...], date_from="2022-01-01", date_to="2023-12-31")
    score = gym.evaluate(weights=ScoringWeights(), top_n=10, hold_days=5)
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        # symbol -> DataFrame with columns: date (index), close, sma50, sma200,
        #           ema12, ema26, bull_market, next_action, atr14_pcnt, adx,
        #           ema_cross, above_sma50, above_sma200, adx_strong,
        #           adx_very_strong, forward_5d
        self._history: Dict[str, pd.DataFrame] = {}
        self._eval_dates: List[pd.Timestamp] = []

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_history(
        self,
        symbols: List[str],
        date_from: str,
        date_to: str,
        progress_cb: Optional[Any] = None,
    ) -> None:
        """
        Fetch EOD + technical indicator history for each symbol via EODHD APIClient.

        Indicators fetched per symbol:
          - EOD OHLCV (for close price and forward-return calculation)
          - SMA50, SMA200 (via get_technical_indicator_data)
          - EMA12, EMA26
          - ATR14 (normalised to % of price)
          - ADX14

        Parameters
        ----------
        symbols : list of ticker strings (without .US suffix)
        date_from : "YYYY-MM-DD"
        date_to : "YYYY-MM-DD"
        progress_cb : optional callable(symbol, i, total) for progress reporting
        """
        try:
            from eodhd import APIClient  # noqa: PLC0415
        except ImportError:
            raise ImportError(
                "eodhd package required for ScreenerGym. pip install eodhd"
            )

        api = APIClient(self._api_key)
        total = len(symbols)

        for i, sym in enumerate(symbols):
            ticker = f"{sym}.US"
            try:
                eod = api.get_eod_historical_stock_market_data(
                    symbol=ticker, period="d", from_date=date_from, to_date=date_to, order="a"
                )
                if eod is None or eod.empty:
                    continue

                def _get_ind(fn: str, **kw: Any) -> Optional[pd.DataFrame]:
                    try:
                        return api.get_technical_indicator_data(
                            ticker=ticker,
                            function=fn,
                            order="a",
                            date_from=date_from,
                            date_to=date_to,
                            **kw,
                        )
                    except Exception:
                        return None

                sma50 = _get_ind("sma", period=50)
                sma200 = _get_ind("sma", period=200)
                ema12 = _get_ind("ema", period=12)
                ema26 = _get_ind("ema", period=26)
                atr14 = _get_ind("atr", period=14)
                adx14 = _get_ind("adx", period=14)

                df = eod[["close"]].copy()
                df.index = pd.to_datetime(df.index)

                def _merge(
                    ind_df: Optional[pd.DataFrame], col_in: str, col_out: str
                ) -> None:
                    if (
                        ind_df is not None
                        and not ind_df.empty
                        and col_in in ind_df.columns
                    ):
                        ind_df.index = pd.to_datetime(ind_df.index)
                        df[col_out] = ind_df[col_in]

                _merge(sma50, "sma", "sma50")
                _merge(sma200, "sma", "sma200")
                _merge(ema12, "ema", "ema12")
                _merge(ema26, "ema", "ema26")
                _merge(atr14, "atr", "atr14")
                _merge(adx14, "adx", "adx14")

                # Derived boolean fields
                df["atr14_pcnt"] = (
                    df.get("atr14", pd.Series(dtype=float)) / df["close"] * 100
                )
                df["ema_cross"] = df.get("ema12", pd.Series(dtype=float)) > df.get(
                    "ema26", pd.Series(dtype=float)
                )
                df["above_sma50"] = df["close"] > df.get("sma50", pd.Series(dtype=float))
                df["above_sma200"] = df["close"] > df.get(
                    "sma200", pd.Series(dtype=float)
                )
                df["adx_strong"] = df.get("adx14", pd.Series(dtype=float)) > 25
                df["adx_very_strong"] = df.get("adx14", pd.Series(dtype=float)) > 40
                df["forward_5d"] = df["close"].shift(-5) / df["close"] - 1.0

                self._history[sym] = df.dropna(subset=["close"])
                log.debug("Loaded history for %s: %d rows", sym, len(df))

            except Exception as exc:
                log.warning("Failed to load history for %s: %s", sym, exc)

            if progress_cb:
                progress_cb(sym, i + 1, total)

        # Build common evaluation dates (union of all symbol dates)
        if self._history:
            all_dates = sorted(
                set.union(*(set(df.index) for df in self._history.values()))
            )
            self._eval_dates = [
                d for d in all_dates if isinstance(d, pd.Timestamp)
            ]
            log.info(
                "ScreenerGym: loaded %d symbols, %d eval dates",
                len(self._history),
                len(self._eval_dates),
            )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_symbol_on_date(
        self, symbol: str, date: pd.Timestamp, weights: ScoringWeights
    ) -> float:
        """Score a single symbol on a specific date using the given weights.

        Parameters
        ----------
        symbol : str
            Ticker without .US suffix.
        date : pd.Timestamp
            Evaluation date.
        weights : ScoringWeights
            Weight configuration to apply.

        Returns
        -------
        float
            Computed score, or 0.0 if the symbol has no data on *date*.
        """
        df = self._history.get(symbol)
        if df is None or date not in df.index:
            return 0.0
        row = df.loc[date]
        s = 0.0

        # bull_market proxy: price > sma200 AND ema_cross (ScannerClient not in gym)
        bull = bool(row.get("above_sma200", False)) and bool(row.get("ema_cross", False))
        if bull:
            s += weights.w_bull_market

        # next_action proxy: ema_cross AND above_sma50
        if bool(row.get("ema_cross", False)) and bool(row.get("above_sma50", False)):
            s += weights.w_next_action

        if bool(row.get("ema_cross", False)):
            s += weights.w_ema_cross

        if bool(row.get("above_sma50", False)):
            s += weights.w_above_sma50

        if bool(row.get("above_sma200", False)):
            s += weights.w_above_sma200

        atr_pct = float(row.get("atr14_pcnt", 0) or 0)
        if atr_pct > 1.0:
            s += weights.w_atr_volatile

        if bool(row.get("adx_strong", False)):
            s += weights.w_adx_strong

        if bool(row.get("adx_very_strong", False)):
            s += weights.w_adx_very_strong

        return s

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        weights: ScoringWeights,
        top_n: int = 10,
        hold_days: int = 5,
        step_days: int = 5,
    ) -> float:
        """
        Evaluate scoring weights against historical data.

        For each evaluation date (every step_days), rank all symbols by score,
        take the top_n, and measure their hold_days forward return.

        Parameters
        ----------
        weights : ScoringWeights
            Weight configuration to evaluate.
        top_n : int
            Number of symbols to select per evaluation date.
        hold_days : int
            Forward-return horizon in calendar days (uses ``forward_5d`` column).
        step_days : int
            Subsample evaluation dates every this many steps.

        Returns
        -------
        float
            Mean forward return of all top-N selections across all evaluation
            dates.  Higher is better (Optuna maximises this).
            Returns -999.0 if insufficient data.
        """
        if not self._history or not self._eval_dates:
            log.warning("ScreenerGym.evaluate called with no loaded history")
            return -999.0

        returns: List[float] = []
        symbols = list(self._history.keys())
        eval_dates = self._eval_dates[::step_days]

        for date in eval_dates:
            scores: List[Tuple[str, float]] = [
                (sym, self.score_symbol_on_date(sym, date, weights))
                for sym in symbols
            ]
            scores.sort(key=lambda x: x[1], reverse=True)

            # Only pick symbols with a positive score
            top_picks = [sym for sym, sc in scores[:top_n] if sc > 0]

            for sym in top_picks:
                df = self._history[sym]
                if date not in df.index:
                    continue
                fwd = df.loc[date].get("forward_5d")
                if fwd is not None and not np.isnan(float(fwd)):
                    returns.append(float(fwd))

        if not returns:
            return -999.0
        return float(np.mean(returns))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Return a summary of loaded history for logging and diagnostics."""
        return {
            "symbols_loaded": len(self._history),
            "eval_dates": len(self._eval_dates),
            "date_range": (
                str(self._eval_dates[0]) if self._eval_dates else None,
                str(self._eval_dates[-1]) if self._eval_dates else None,
            ),
        }

"""
modules/grid_trading/grid_calculator_v2.py

Enhanced grid calculator ported from research/gym_origin/trading_algorithms.py.

This module lifts the rich grid logic out of the ``TradingAlgorithm`` class
(which depended on a gymnasium ``TradingEnv``) and exposes it as pure
functions that accept a plain pandas DataFrame and explicit numeric parameters.

No gymnasium / gym.Env imports are present anywhere in this file.
All TA-Lib calls are wrapped in ``try/except ImportError`` so the module
works even when TA-Lib is not installed, falling back to pandas/numpy
equivalents.

Original source references
--------------------------
- ``calculate_midprice``                         -> line 522
- ``generate_grid`` (linear + log auto-adjust)   -> lines 987, 1000
- ``auto_set_grid_parameters``                   -> line 1057
- ``auto_set_period``                            -> line 1152
- ``fixed_percentage_of_capital``                -> line 144
- ``fixed_number_of_shares``                     -> line 167
- ``dynamic_allocation_based_on_grid_distance``  -> line 171
- ``proportional_allocation_based_on_remaining_capital`` -> line 179
- ``volatility_based_allocation``                -> line 187

Ported: LATS Phase-2, 2026-03-21
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("algo_system.grid_calculator_v2")

# ---------------------------------------------------------------------------
# Optional TA-Lib import
# ---------------------------------------------------------------------------
try:
    import talib as _talib  # type: ignore

    _TALIB_AVAILABLE = True
except ImportError:
    _talib = None  # type: ignore
    _TALIB_AVAILABLE = False
    logger.warning(
        "TA-Lib not found. Falling back to pandas/numpy equivalents for all "
        "indicator calculations.  Results may differ slightly from TA-Lib."
    )


# ---------------------------------------------------------------------------
# Module-level constants (importable by optuna_grid_optimizer, tests, etc.)
# ---------------------------------------------------------------------------

VALID_METHODS: tuple[str, ...] = (
    "market_price", "moving_average", "mid_high_low", "vwap"
)

VALID_ALLOCATION_STRATEGIES: tuple[str, ...] = (
    "fixed_pct", "fixed_shares", "dynamic_grid", "proportional", "volatility"
)

VALID_INDICATORS: tuple[str, ...] = (
    "ATR", "RSI", "BollingerBands", "SMA", "EMA",
    "StdDev", "CyclePeriod", "CyclePhase", "TrendMode",
)


# ===========================================================================
# GridConfigV2 — single source of truth for all Optuna-tunable parameters
# ===========================================================================


@dataclass
class GridConfigV2:
    """
    All Optuna-tunable configuration parameters for the v2 grid engine.

    Each field corresponds to a parameter that was previously scattered across
    ``TradingAlgorithm.grid_trading()`` keyword arguments.  Consolidating them
    in one dataclass makes hyperparameter search trivial: Optuna constructs a
    ``GridConfigV2`` per trial and passes it to the LATS strategy.

    Attributes
    ----------
    grid_distance : float
        Fractional or absolute spacing between adjacent grid levels
        (e.g. ``0.01`` = 1 %).
        Replaces the ``grid_distance`` kwarg in ``TradingAlgorithm.grid_trading()``.
    grid_range : float
        Total price range to cover with the grid, symmetric around the
        midprice.
        Replaces the ``grid_range`` kwarg.
    method : str
        Midprice calculation method.  One of:
        ``"market_price"`` | ``"moving_average"`` | ``"mid_high_low"`` |
        ``"vwap"``.
        Replaces the ``method`` kwarg.
    period : int
        Look-back window used for all rolling calculations.
        Replaces the ``period`` kwarg.
    dynamic_midprice : bool
        When ``True`` the midprice (and therefore the grid) is recalculated
        periodically and on large price deviations.
        Replaces the ``dynamic_midprice`` kwarg.
    midprice_adjust_period : int
        How many steps between forced midprice recalculations when
        ``dynamic_midprice`` is ``True``.
        Replaces the ``midprice_adjust_period`` kwarg.
    auto_adjust : bool
        When ``True`` the grid uses logarithmic scaling across the full
        historical High/Low range from ``df`` rather than linearly around the
        midprice.
        Replaces the ``auto_adjust`` kwarg in ``generate_grid()``.
    auto_set_grid : bool
        When ``True``, ``auto_set_grid_parameters()`` is called first to
        derive ``grid_range`` and ``grid_distance`` from indicators.
        Replaces the ``use_indicators`` kwarg in ``generate_grid()``.
    auto_set_period : bool
        When ``True`` the period is derived from the Hilbert-Transform dominant
        cycle (TA-Lib ``HT_DCPERIOD``) or a numpy autocorrelation fallback.
        Replaces the ``auto_set_period`` kwarg in ``auto_set_grid_parameters()``.
    allocation_strategy : Literal
        Which allocation function is called on a buy signal.
        Replaces the ``allocation_strategy`` kwarg.
    allocation_param : float
        Primary numeric argument forwarded to the active allocation function
        (percentage for ``"fixed_pct"``, share count for ``"fixed_shares"``,
        etc.).
        Replaces the ``allocation_param`` kwarg.
    volatility_threshold : float
        Normalised-ATR threshold used by the ``"volatility"`` allocation
        strategy to reduce position size in high-vol regimes.
        Replaces the ``volatility_threshold`` kwarg.
    entry_multiplier : float
        Multiplier applied to position size on the first market entry.
        Replaces the ``entry_multiplier`` kwarg.
    deviation_threshold : float
        Fraction of ``grid_range``; when ``|current_price - midprice|``
        exceeds this, the midprice is recalculated.
        Replaces the ``deviation_threshold`` kwarg.
    selected_indicators : list[str]
        Which indicators feed into ``auto_set_grid_parameters()``.
        Supported values: ``"ATR"``, ``"RSI"``, ``"BollingerBands"``,
        ``"StdDev"``, ``"SMA"``, ``"EMA"``, ``"CyclePeriod"``,
        ``"CyclePhase"``, ``"TrendMode"``.
        Replaces the ``selected_indicators`` kwarg.
    log_scale : bool
        When ``True``, ``generate_grid_levels()`` uses logarithmic spacing
        even without ``auto_adjust``.  Equivalent to using
        ``generate_logarithmic_grid()`` from the original.
    """

    grid_distance: float = 0.01
    grid_range: float = 0.20
    method: Literal[
        "market_price", "moving_average", "mid_high_low", "vwap"
    ] = "moving_average"
    period: int = 20

    dynamic_midprice: bool = True
    midprice_adjust_period: int = 5

    auto_adjust: bool = False
    auto_set_grid: bool = False
    auto_set_period: bool = False

    allocation_strategy: Literal[
        "fixed_pct", "fixed_shares", "dynamic_grid", "proportional", "volatility"
    ] = "fixed_pct"
    allocation_param: float = 0.30

    volatility_threshold: float = 0.02
    entry_multiplier: float = 2.0
    deviation_threshold: float = 0.50

    selected_indicators: List[str] = field(default_factory=list)
    log_scale: bool = False
    history_lookback: int = 30  # days of price history used for price filter / ATR base

    def __post_init__(self) -> None:
        if self.method not in VALID_METHODS:
            raise ValueError(f"Invalid method {self.method!r}. Choose from {VALID_METHODS}")
        if self.allocation_strategy not in VALID_ALLOCATION_STRATEGIES:
            raise ValueError(
                f"Invalid allocation_strategy {self.allocation_strategy!r}. "
                f"Choose from {VALID_ALLOCATION_STRATEGIES}"
            )
        unknown = [i for i in self.selected_indicators if i not in VALID_INDICATORS]
        if unknown:
            raise ValueError(f"Unknown indicator(s): {unknown}. Valid: {VALID_INDICATORS}")

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    def grid_count(self) -> int:
        """Number of grid levels (approximate)."""
        if self.grid_distance <= 0:
            return 0
        return max(1, int(round(self.grid_range / self.grid_distance)))

    def upper_bound(self, midprice: float) -> float:
        """Upper grid boundary given a midprice."""
        return midprice + self.grid_range / 2.0

    def lower_bound(self, midprice: float) -> float:
        """Lower grid boundary given a midprice."""
        return midprice - self.grid_range / 2.0

    def to_dict(self) -> dict:
        """Serialise to a JSON-safe dict (for shared_state persistence)."""
        import dataclasses
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "GridConfigV2":
        """Deserialise from a dict, ignoring unknown keys (forward-compat)."""
        import dataclasses
        known = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ===========================================================================
# Internal helpers: pandas / numpy fallbacks for TA-Lib functions
# ===========================================================================


def _atr_numpy(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> pd.Series:
    """Average True Range via pandas (no TA-Lib required).

    Uses Wilder's smoothing (simple rolling mean for simplicity, matching the
    common pandas interpretation used when TA-Lib is unavailable).
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _rsi_numpy(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-smoothed RSI implemented in pandas (no TA-Lib)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _bbands_numpy(
    close: pd.Series,
    period: int = 20,
    nbdev: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands in pandas — returns ``(upper, middle, lower)``."""
    middle = close.rolling(window=period, min_periods=1).mean()
    std = close.rolling(window=period, min_periods=1).std(ddof=0)
    upper = middle + nbdev * std
    lower = middle - nbdev * std
    return upper, middle, lower


def _stddev_numpy(close: pd.Series, period: int = 20) -> pd.Series:
    """Rolling population standard deviation (``ddof=0``) to match TA-Lib STDDEV."""
    return close.rolling(window=period, min_periods=1).std(ddof=0)


def _ht_dcperiod_numpy(close: pd.Series) -> pd.Series:
    """
    Approximate dominant cycle period via autocorrelation (no TA-Lib).

    TA-Lib ``HT_DCPERIOD`` applies the Hilbert Transform, which is complex to
    replicate faithfully in pure NumPy.  This fallback finds the lag (2–40)
    that maximises the absolute Pearson correlation of the series with itself,
    which is a reasonable proxy for dominant cycle detection.

    Returns a constant ``pd.Series`` (same index as ``close``) so callers can
    use ``.mean()`` identically to the TA-Lib output.
    """
    best_lag = 20
    best_corr = 0.0
    values = close.values
    if len(values) >= 42:  # need enough samples for lag=40
        for lag in range(2, 41):
            corr = float(np.corrcoef(values[lag:], values[:-lag])[0, 1])
            if not np.isnan(corr) and abs(corr) > best_corr:
                best_corr = abs(corr)
                best_lag = lag
    return pd.Series(float(best_lag), index=close.index)


# ===========================================================================
# 1. auto_set_period
# ===========================================================================


def auto_set_period(df: pd.DataFrame) -> int:
    """
    Derive the dominant look-back period from the close price series.

    Ported from ``TradingAlgorithm.auto_set_period()`` (line 1152 of
    ``trading_algorithms.py``).  The original called
    ``talib.HT_DCPERIOD(data["Close"]).mean()`` and returned
    ``max(3, int(cycle_period))``.  This function replicates that behaviour
    exactly, with a numpy autocorrelation fallback when TA-Lib is absent.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.  Must contain a ``"Close"`` column.

    Returns
    -------
    int
        Auto-detected period, minimum 3 (mirrors the original's
        ``max(3, int(...))``) to avoid degenerate small values.
    """
    close = df["Close"]
    if _TALIB_AVAILABLE:
        cycle_series = _talib.HT_DCPERIOD(close)
    else:
        cycle_series = _ht_dcperiod_numpy(close)

    valid = cycle_series.dropna()
    raw_mean = float(valid.mean()) if not valid.empty else 20.0
    period = max(3, int(raw_mean))
    logger.info("auto_set_period: derived period=%d", period)
    return period


# ===========================================================================
# 2. auto_set_grid_parameters
# ===========================================================================


def auto_set_grid_parameters(
    df: pd.DataFrame,
    period: int = 20,
    selected_indicators: Optional[List[str]] = None,
    auto_set_period_flag: bool = False,
) -> Tuple[float, float]:
    """
    Derive ``grid_range`` and ``grid_distance`` from historical data and
    optional indicator values.

    Ported from ``TradingAlgorithm.auto_set_grid_parameters()`` (line 1057 of
    ``trading_algorithms.py``).  The original read ``self.env.data``; here
    we accept ``df`` directly so the function is environment-free.

    Algorithm
    ---------
    1. Optionally auto-detect ``period`` via :func:`auto_set_period`.
    2. Compute **baseline** values:

       * ``grid_range  = rolling_max(High, period) - rolling_min(Low, period)``
       * ``grid_distance = (log10(High) - log10(Low)) / 10``  (log-space)

    3. For each indicator in ``selected_indicators``, compute its mean and
       accumulate into ``indicator_values``.
    4. Combine as ``combined = mean(indicator_values)`` and scale both
       ``grid_range`` and ``grid_distance`` by ``(1 + combined / 100)``.
    5. Return ``(grid_range, 10 ** grid_distance)`` to undo the log-space
       compression on distance (mirrors the original's final
       ``return grid_range, 10**grid_distance``).

    Supported indicator names
    -------------------------
    ``"ATR"``, ``"RSI"``, ``"BollingerBands"``, ``"StdDev"``,
    ``"SMA"``, ``"EMA"``, ``"CyclePeriod"``, ``"CyclePhase"``,
    ``"TrendMode"``.

    Note: ``"CyclePhase"`` and ``"TrendMode"`` have no pandas equivalent.
    When TA-Lib is unavailable they are silently skipped with a warning logged.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.  Must have columns ``"High"``, ``"Low"``,
        ``"Close"``.
    period : int
        Look-back window in candles.  Overridden when
        ``auto_set_period_flag`` is ``True``.
    selected_indicators : list[str] or None
        Indicators to include.  Pass ``None`` or ``[]`` for the baseline
        only.
    auto_set_period_flag : bool
        When ``True``, derive ``period`` via :func:`auto_set_period` before
        running indicator calculations.

    Returns
    -------
    tuple[float, float]
        ``(grid_range, grid_distance)`` where ``grid_distance`` has been
        exponentiated out of log-space.
    """
    if selected_indicators is None:
        selected_indicators = []

    if auto_set_period_flag:
        period = auto_set_period(df)

    # Work on a local copy to avoid mutating the caller's DataFrame
    data = df.copy()
    data["_Return"] = data["Close"].pct_change()

    historical_high = float(
        data["High"].rolling(window=period, min_periods=1).max().iloc[-1]
    )
    historical_low = float(
        data["Low"].rolling(window=period, min_periods=1).min().iloc[-1]
    )

    grid_range = historical_high - historical_low

    # Guard against log(0)
    safe_high = max(historical_high, 1e-10)
    safe_low = max(historical_low, 1e-10)
    log_high = np.log10(safe_high)
    log_low = np.log10(safe_low)
    grid_distance_log = (log_high - log_low) / 10.0

    indicator_values: List[float] = []

    if selected_indicators:
        close = data["Close"]
        high = data["High"]
        low = data["Low"]

        if "ATR" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.ATR(high, low, close, timeperiod=period)
            else:
                series = _atr_numpy(high, low, close, period)
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if "RSI" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.RSI(close, timeperiod=period)
            else:
                series = _rsi_numpy(close, period=period)
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if "CyclePeriod" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.HT_DCPERIOD(close)
            else:
                series = _ht_dcperiod_numpy(close)
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if "CyclePhase" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.HT_DCPHASE(close)
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))
            else:
                logger.warning(
                    "auto_set_grid_parameters: 'CyclePhase' requires TA-Lib; "
                    "skipping this indicator."
                )

        if "TrendMode" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.HT_TRENDMODE(close)
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))
            else:
                logger.warning(
                    "auto_set_grid_parameters: 'TrendMode' requires TA-Lib; "
                    "skipping this indicator."
                )

        if "BollingerBands" in selected_indicators:
            if _TALIB_AVAILABLE:
                upper, middle, lower = _talib.BBANDS(
                    close, timeperiod=period, nbdevup=2, nbdevdn=2
                )
            else:
                upper, middle, lower = _bbands_numpy(close, period=period, nbdev=2.0)
            mid_mean = float(middle.mean())
            if mid_mean != 0:
                bb_value = (float(upper.mean()) - float(lower.mean())) / mid_mean
                indicator_values.append(bb_value)

        if "SMA" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.SMA(close, timeperiod=period)
            else:
                series = close.rolling(window=period, min_periods=1).mean()
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if "EMA" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.EMA(close, timeperiod=period)
            else:
                series = close.ewm(span=period, adjust=False).mean()
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if "StdDev" in selected_indicators:
            if _TALIB_AVAILABLE:
                series = _talib.STDDEV(close, timeperiod=period, nbdev=1)
            else:
                series = _stddev_numpy(close, period=period)
            valid = series.dropna()
            if not valid.empty:
                indicator_values.append(float(valid.mean()))

        if indicator_values:
            combined_value = float(np.mean(indicator_values))
            scale = 1.0 + combined_value / 100.0
            grid_range *= scale
            grid_distance_log *= scale

    result_distance = 10.0 ** grid_distance_log  # undo log-space compression

    logger.info(
        "auto_set_grid_parameters: period=%d  grid_range=%.4f  grid_distance=%.6f",
        period,
        grid_range,
        result_distance,
    )
    return grid_range, result_distance


# ===========================================================================
# 3. calculate_midprice
# ===========================================================================


def calculate_midprice(
    df: pd.DataFrame,
    current_step: int,
    method: str = "moving_average",
    period: int = 20,
    grid: Optional[np.ndarray] = None,
    auto_adjust: bool = False,
) -> float:
    """
    Calculate the midprice at a given candle index in the OHLCV DataFrame.

    Ported from ``TradingAlgorithm.calculate_midprice()`` (line 522 of
    ``trading_algorithms.py``).  The original accessed ``self.env.data`` and
    ``self.env.current_step``; here they are explicit function arguments.

    Midprice methods
    ----------------
    ``"market_price"``
        Close price exactly ``period`` candles ago (index ``period - 1``).
        Requires ``current_step >= period``; raises ``ValueError`` otherwise.

    ``"moving_average"``
        Rolling mean of ``"Close"`` up to and including ``current_step``,
        with a look-back window of ``period``.

    ``"mid_high_low"``
        ``(rolling_max("High", period) + rolling_min("Low", period)) / 2``
        evaluated at ``current_step``.

    ``"vwap"``
        Volume-weighted average price computed over the last ``period``
        candles ending at ``current_step``.

    ``auto_adjust=True``
        Ignores ``method`` and returns the median element of ``grid`` when
        provided.  Matches the auto-adjust branch in the original method.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.  Must contain ``"Close"``, ``"High"``, ``"Low"``,
        ``"Volume"`` columns.
    current_step : int
        Integer row index (iloc position) of the current candle.
    method : str
        One of ``"market_price"``, ``"moving_average"``, ``"mid_high_low"``,
        ``"vwap"``.
    period : int
        Rolling window size in candles.
    grid : np.ndarray or None
        Pre-generated grid levels.  Only used when ``auto_adjust=True``.
    auto_adjust : bool
        When ``True`` and ``grid`` is non-empty, return the grid's midpoint
        element.

    Returns
    -------
    float
        Calculated midprice.  Falls back to ``df["Close"].iloc[0]`` on any
        error, matching the original's ``except`` fallback.
    """
    try:
        if auto_adjust and grid is not None and len(grid) > 0:
            midprice = float(grid[len(grid) // 2])
            logger.info(
                "calculate_midprice: auto_adjust -> midprice=%.4f", midprice
            )
            return midprice

        if method == "market_price":
            if current_step >= period:
                midprice = float(df["Close"].iloc[period - 1])
            else:
                raise ValueError(
                    f"current_step ({current_step}) < period ({period}); "
                    "cannot use 'market_price' method."
                )

        elif method == "moving_average":
            midprice = float(
                df["Close"]
                .rolling(window=period, min_periods=1)
                .mean()
                .iloc[current_step]
            )

        elif method == "mid_high_low":
            roll_high = float(
                df["High"]
                .rolling(window=period, min_periods=1)
                .max()
                .iloc[current_step]
            )
            roll_low = float(
                df["Low"]
                .rolling(window=period, min_periods=1)
                .min()
                .iloc[current_step]
            )
            midprice = (roll_high + roll_low) / 2.0

        elif method == "vwap":
            price = df["Close"]
            volume = df["Volume"]
            pv_sum = (
                (price * volume)
                .rolling(window=period, min_periods=1)
                .sum()
                .iloc[current_step]
            )
            v_sum = (
                volume
                .rolling(window=period, min_periods=1)
                .sum()
                .iloc[current_step]
            )
            if v_sum == 0.0:
                raise ValueError(
                    "Volume sum is zero at current_step=%d; cannot compute VWAP."
                    % current_step
                )
            midprice = float(pv_sum / v_sum)

        else:
            raise ValueError(
                "Unknown midprice method '%s'.  "
                "Choose from: 'market_price', 'moving_average', "
                "'mid_high_low', 'vwap'." % method
            )

        if np.isnan(midprice):
            raise ValueError(
                "Calculated midprice is NaN — check data and method parameters."
            )

    except Exception as exc:
        logger.error(
            "calculate_midprice: %s — falling back to df['Close'].iloc[0]", exc
        )
        midprice = float(df["Close"].iloc[0])

    return midprice


# ===========================================================================
# 4. generate_grid_levels
# ===========================================================================


def generate_grid_levels(
    midprice: float,
    grid_distance: float,
    grid_range: float,
    auto_adjust: bool = False,
    log_scale: bool = False,
    df: Optional[pd.DataFrame] = None,
    num_log_levels: int = 10,
) -> np.ndarray:
    """
    Generate a sorted NumPy array of grid price levels.

    Combines the logic of ``TradingAlgorithm.generate_grid()`` (line 1000)
    and ``TradingAlgorithm.generate_logarithmic_grid()`` (line 987).

    Two spacing modes
    -----------------
    **Linear (default)**
        ``np.arange(midprice - grid_range/2, midprice + grid_range/2, grid_distance)``
        Mirrors the ``else`` branch in the original ``generate_grid()``.

    **Logarithmic** (``auto_adjust=True`` **or** ``log_scale=True``)
        ``np.logspace(log10(df["Low"].min()), log10(df["High"].max()), num_log_levels)``
        Mirrors the ``auto_adjust`` branch in ``generate_grid()`` and
        ``generate_logarithmic_grid()``.  Requires ``df`` to be supplied.

    Parameters
    ----------
    midprice : float
        Central reference price.  Used only in linear mode.
    grid_distance : float
        Step size between adjacent levels in linear mode.  Must be > 0.
    grid_range : float
        Total price band width in linear mode (symmetric around
        ``midprice``).
    auto_adjust : bool
        Activates log-space mode using the full historical High/Low range
        from ``df``.
    log_scale : bool
        Alias for ``auto_adjust``; either flag enables logarithmic mode.
    df : pd.DataFrame or None
        Required when ``auto_adjust`` or ``log_scale`` is ``True``.
        Must have ``"High"`` and ``"Low"`` columns.
    num_log_levels : int
        Number of levels to generate in logarithmic mode (default 10).

    Returns
    -------
    np.ndarray
        Sorted array of grid price levels.  An empty array is returned (and
        a warning is logged) when no levels are produced, matching the
        original's zero-length guard.

    Raises
    ------
    ValueError
        If ``auto_adjust`` or ``log_scale`` is ``True`` but ``df`` is ``None``.
    ValueError
        If ``grid_distance <= 0`` in linear mode.
    """
    use_log = auto_adjust or log_scale

    if use_log:
        if df is None:
            raise ValueError(
                "generate_grid_levels: 'df' must be supplied when "
                "auto_adjust=True or log_scale=True."
            )
        min_price = float(df["Low"].min())
        max_price = float(df["High"].max())
        if min_price <= 0:
            logger.warning(
                "generate_grid_levels: min_price=%.6f <= 0; clamping to 1e-8",
                min_price,
            )
            min_price = 1e-8
        log_start = np.log10(min_price)
        log_end = np.log10(max_price)
        grid = np.logspace(log_start, log_end, num=num_log_levels)
        logger.info(
            "generate_grid_levels: log-scale  min=%.4f  max=%.4f  levels=%d",
            min_price,
            max_price,
            num_log_levels,
        )
    else:
        if grid_distance <= 0:
            raise ValueError(
                "generate_grid_levels: grid_distance must be > 0, "
                "got %.6f." % grid_distance
            )
        grid_start = midprice - grid_range / 2.0
        grid_end = midprice + grid_range / 2.0
        grid = np.arange(grid_start, grid_end, grid_distance)
        logger.debug(
            "generate_grid_levels: linear  start=%.4f  end=%.4f  step=%.6f",
            grid_start,
            grid_end,
            grid_distance,
        )

    if len(grid) == 0:
        logger.error(
            "generate_grid_levels: no levels were generated — "
            "check grid_distance, grid_range, or price data."
        )

    return np.sort(grid)


# ===========================================================================
# 5. Allocation strategy pure functions
# ===========================================================================


def alloc_fixed_pct(
    balance: float,
    price: float,
    pct: float = 0.30,
    default_pct: float = 0.30,
) -> int:
    """
    Buy as many whole shares as ``pct`` of ``balance`` can afford at ``price``.

    Ported from ``TradingAlgorithm.fixed_percentage_of_capital()`` (line 144).
    The original read balance and price from ``self.env``; here they are
    explicit arguments.

    Original formula::

        max_shares = int((balance * percentage) // close_price)

    Parameters
    ----------
    balance : float
        Available cash balance.
    price : float
        Current asset price (close at the current candle).
    pct : float
        Fraction of balance to allocate, e.g. ``0.30`` = 30 %.  When
        ``None`` or non-positive, ``default_pct`` is used.
    default_pct : float
        Fallback allocation fraction.

    Returns
    -------
    int
        Maximum number of whole shares that can be purchased, >= 0.
    """
    if not pct or pct <= 0:
        logger.warning(
            "alloc_fixed_pct: invalid pct=%s; using default=%.2f", pct, default_pct
        )
        pct = default_pct

    if price <= 0:
        logger.error("alloc_fixed_pct: price must be > 0, got %.6f", price)
        return 0

    shares = int((balance * pct) // price)
    return max(0, shares)


def alloc_fixed_shares(n: float) -> int:
    """
    Return a fixed number of shares regardless of price or balance.

    Ported from ``TradingAlgorithm.fixed_number_of_shares()`` (line 167).
    The original returned ``fixed_shares`` directly; this version truncates
    to ``int`` and guards against negative values.

    Parameters
    ----------
    n : float
        Number of shares to trade.  Truncated to int.

    Returns
    -------
    int
        Fixed share count, >= 0.
    """
    return max(0, int(n))


def alloc_dynamic_grid(
    balance: float,
    price: float,
    grid_level_idx: int,
    total_levels: int,
    pct: float = 1.0,
) -> int:
    """
    Allocate more shares the deeper (lower) into the grid the current level is.

    Ported from
    ``TradingAlgorithm.dynamic_allocation_based_on_grid_distance()``
    (line 171).  The original read balance/price from ``self.env`` and assumed
    ``grid_level`` was the index iterated in the buy loop; here they are
    explicit arguments.

    Formula::

        remaining = total_levels - grid_level_idx
        max_affordable = int(balance // price)
        shares = max(1, int(max_affordable * (remaining / total_levels)))

    Parameters
    ----------
    balance : float
        Available cash balance.
    price : float
        Current asset price.
    grid_level_idx : int
        Zero-based index of the current grid level being evaluated.
    total_levels : int
        Total number of grid levels.
    pct : float
        Unused in the original; retained for API consistency.

    Returns
    -------
    int
        Number of shares to trade, at least 1.
    """
    if price <= 0:
        logger.error("alloc_dynamic_grid: price must be > 0")
        return 0

    max_affordable = int(balance // price)
    remaining = max(1, total_levels - grid_level_idx)
    shares = max(1, int(max_affordable * (remaining / max(1, total_levels))))
    return shares


def alloc_proportional(
    balance: float,
    price: float,
    remaining_levels: int,
    pct: float = 1.0,
) -> int:
    """
    Spread the remaining balance equally across the remaining grid levels.

    Ported from
    ``TradingAlgorithm.proportional_allocation_based_on_remaining_capital()``
    (line 179).  The original read balance/price from ``self.env``.

    Each level receives ``balance / remaining_levels`` dollars; this function
    returns how many whole shares that budget buys at ``price``.

    Formula::

        budget_per_level = balance / remaining_levels
        shares = int(budget_per_level // price)

    Parameters
    ----------
    balance : float
        Available cash balance.
    price : float
        Current asset price.
    remaining_levels : int
        Number of grid levels not yet filled (including the current one).
    pct : float
        Unused; retained for API consistency.

    Returns
    -------
    int
        Number of shares allocated to this level, >= 0.
    """
    if remaining_levels <= 0:
        logger.warning(
            "alloc_proportional: remaining_levels=%d <= 0; returning 0",
            remaining_levels,
        )
        return 0
    if price <= 0:
        logger.error("alloc_proportional: price must be > 0")
        return 0

    budget_per_level = balance / remaining_levels
    shares = int(budget_per_level // price)
    return max(0, shares)


def alloc_volatility(
    balance: float,
    price: float,
    atr: float,
    atr_multiplier: float = 1.0,
    max_pct: float = 1.0,
    volatility_threshold: float = 0.02,
    high_volatility_reduction: float = 0.5,
) -> int:
    """
    Reduce position size when rolling volatility exceeds a threshold.

    Ported from ``TradingAlgorithm.volatility_based_allocation()`` (line 187).
    The original computed volatility inline from ``self.env.data`` using a
    20-bar rolling pct-change std; here the caller pre-computes ``atr`` and
    passes it in so the function remains pure.

    Behaviour
    ---------
    Normalised volatility = ``(atr * atr_multiplier) / price``.

    * If normalised vol > ``volatility_threshold``
      → trade ``int(max_affordable * high_volatility_reduction)`` shares.
    * Otherwise → trade ``max_affordable`` shares.

    Parameters
    ----------
    balance : float
        Available cash balance.
    price : float
        Current asset price.
    atr : float
        Current Average True Range value in the same units as ``price``.
    atr_multiplier : float
        Multiplier applied to ``atr`` before normalising.  Pass ``1.0`` to
        replicate the original exactly.
    max_pct : float
        Upper bound on the fraction of balance to deploy (default 1.0).
    volatility_threshold : float
        Normalised-volatility level above which the position is reduced.
        Replaces the ``volatility_threshold`` kwarg.
    high_volatility_reduction : float
        Multiplier (<1) applied to share count in high-volatility regimes.
        Replaces the ``high_volatility_reduction`` kwarg in the original.

    Returns
    -------
    int
        Number of shares to trade, >= 0.
    """
    if price <= 0:
        logger.error("alloc_volatility: price must be > 0")
        return 0

    max_affordable = int((balance * max_pct) // price)
    normalised_vol = (atr * atr_multiplier) / price

    if normalised_vol > volatility_threshold:
        shares = int(max_affordable * high_volatility_reduction)
        logger.debug(
            "alloc_volatility: high vol=%.4f > threshold=%.4f; "
            "reduced shares %d -> %d",
            normalised_vol,
            volatility_threshold,
            max_affordable,
            shares,
        )
    else:
        shares = max_affordable

    return max(0, shares)


# ===========================================================================
# 6. Allocation dispatcher — choose function from GridConfigV2
# ===========================================================================


def allocate_shares(
    config: GridConfigV2,
    balance: float,
    price: float,
    grid_level_idx: int = 0,
    total_levels: int = 1,
    remaining_levels: int = 1,
    atr: float = 0.0,
) -> int:
    """
    Dispatch to the correct allocation function based on
    ``config.allocation_strategy``.

    This convenience wrapper is intended for use inside the LATS strategy
    execute loop.  It collects all possible arguments and forwards the
    relevant subset to the active allocation function.

    Parameters
    ----------
    config : GridConfigV2
        Active grid configuration.
    balance : float
        Current available cash balance.
    price : float
        Current asset price.
    grid_level_idx : int
        Zero-based index of the current grid level.  Used by
        ``"dynamic_grid"``.
    total_levels : int
        Total number of grid levels.  Used by ``"dynamic_grid"``.
    remaining_levels : int
        Unfilled levels remaining including the current one.  Used by
        ``"proportional"``.
    atr : float
        Current ATR value in price units.  Used by ``"volatility"``.

    Returns
    -------
    int
        Number of shares to trade, >= 0.

    Raises
    ------
    ValueError
        If ``config.allocation_strategy`` is not one of the five known values.
    """
    strategy = config.allocation_strategy
    param = config.allocation_param

    if strategy == "fixed_pct":
        return alloc_fixed_pct(balance, price, pct=param)

    if strategy == "fixed_shares":
        return alloc_fixed_shares(param)

    if strategy == "dynamic_grid":
        return alloc_dynamic_grid(
            balance, price, grid_level_idx, total_levels, pct=param
        )

    if strategy == "proportional":
        return alloc_proportional(balance, price, remaining_levels, pct=param)

    if strategy == "volatility":
        return alloc_volatility(
            balance,
            price,
            atr=atr,
            volatility_threshold=config.volatility_threshold,
        )

    raise ValueError(
        "allocate_shares: unknown allocation_strategy '%s'.  "
        "Valid values: 'fixed_pct', 'fixed_shares', 'dynamic_grid', "
        "'proportional', 'volatility'." % strategy
    )


# ===========================================================================
# 7. High-level grid builder — combines midprice + level generation
# ===========================================================================


def build_grid(
    df: pd.DataFrame,
    current_step: int,
    config: GridConfigV2,
) -> Tuple[float, np.ndarray]:
    """
    Build a complete grid: compute midprice then generate all price levels.

    Combines :func:`calculate_midprice` and :func:`generate_grid_levels` in
    the same sequence as the original ``grid_trading()`` loop (lines 630–651
    of ``trading_algorithms.py``), respecting the ``auto_adjust``,
    ``auto_set_grid``, and ``log_scale`` flags from ``GridConfigV2``.

    When ``config.auto_set_grid`` is ``True``, :func:`auto_set_grid_parameters`
    is called first to derive ``grid_range`` and ``grid_distance`` from
    indicators (matching the original ``use_indicators=True`` branch).

    When ``config.auto_adjust`` or ``config.log_scale`` is ``True``, the grid
    is built in log-space across the full historical High/Low range and the
    midprice is set to the median element of those levels.

    In all other cases (linear mode), the grid is centred on the computed
    midprice.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame.
    current_step : int
        Current candle index (iloc position).
    config : GridConfigV2
        All tunable grid parameters.

    Returns
    -------
    tuple[float, np.ndarray]
        ``(midprice, grid_levels)`` where ``grid_levels`` is a sorted NumPy
        array.
    """
    grid_range = config.grid_range
    grid_distance = config.grid_distance

    # Step 1 — optionally derive range/distance from indicators
    if config.auto_set_grid:
        grid_range, grid_distance = auto_set_grid_parameters(
            df,
            period=config.period,
            selected_indicators=config.selected_indicators,
            auto_set_period_flag=config.auto_set_period,
        )

    use_log = config.auto_adjust or config.log_scale

    if use_log:
        # Log-space: grid built from historical extremes; midprice = median level
        grid_levels = generate_grid_levels(
            midprice=0.0,  # unused in log mode
            grid_distance=grid_distance,
            grid_range=grid_range,
            auto_adjust=True,
            log_scale=False,
            df=df,
        )
        midprice = calculate_midprice(
            df=df,
            current_step=current_step,
            method=config.method,
            period=config.period,
            grid=grid_levels,
            auto_adjust=True,
        )
    else:
        # Linear: compute midprice first, then centre the grid on it
        midprice = calculate_midprice(
            df=df,
            current_step=current_step,
            method=config.method,
            period=config.period,
            grid=None,
            auto_adjust=False,
        )
        grid_levels = generate_grid_levels(
            midprice=midprice,
            grid_distance=grid_distance,
            grid_range=grid_range,
            auto_adjust=False,
            log_scale=False,
        )

    logger.info(
        "build_grid: step=%d  midprice=%.4f  levels=%d  range=[%.4f, %.4f]",
        current_step,
        midprice,
        len(grid_levels),
        float(grid_levels[0]) if len(grid_levels) else float("nan"),
        float(grid_levels[-1]) if len(grid_levels) else float("nan"),
    )

    return midprice, grid_levels


# ===========================================================================
# 8. Midprice-recalculation trigger check
# ===========================================================================


def should_recalculate_midprice(
    current_step: int,
    current_price: float,
    midprice: float,
    config: GridConfigV2,
) -> bool:
    """
    Return ``True`` when the grid and midprice should be recalculated.

    Mirrors the conditional from ``TradingAlgorithm.grid_trading()`` (line 710
    of ``trading_algorithms.py``)::

        if dynamic_midprice and (
            current_step % midprice_adjust_period == 0
            or abs(current_price - midprice) > deviation_threshold * grid_range
        ):

    Parameters
    ----------
    current_step : int
        Current candle index.
    current_price : float
        Latest close price.
    midprice : float
        Currently active midprice.
    config : GridConfigV2
        Active configuration.

    Returns
    -------
    bool
        ``True`` when a recalculation is warranted.
    """
    if not config.dynamic_midprice:
        return False

    period_trigger = (current_step % config.midprice_adjust_period) == 0
    deviation_trigger = (
        abs(current_price - midprice) > config.deviation_threshold * config.grid_range
    )
    triggered = period_trigger or deviation_trigger

    if triggered:
        logger.debug(
            "should_recalculate_midprice: step=%d  period_trigger=%s  "
            "deviation_trigger=%s",
            current_step,
            period_trigger,
            deviation_trigger,
        )

    return triggered

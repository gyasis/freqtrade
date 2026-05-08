"""
modules/grid_trading/grid_calculator.py
=======================================
Unified, stateless grid calculator for the LATS algo system.

Replaces both:
  * the old V1 ``GridCalculator`` (lower/upper/count API), and
  * the V2 module-level functions in ``grid_calculator_legacy.py``
    (``generate_grid_levels``, ``calculate_midprice``, etc.)

All methods are ``@staticmethod`` — instances carry no state. ``GridConfig``
(from ``algo_system.config.grid_config``) is the single source of truth for
all tunable parameters.

TA-Lib is optional: every TA-Lib call is wrapped in ``try/except ImportError``
and a pandas/numpy fallback is used when TA-Lib is not installed. Fallbacks
are logged at WARNING level when first taken.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from typing import List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from algo_system.config.grid_config import GridConfig

logger = logging.getLogger("algo_system.grid_calculator")


# ---------------------------------------------------------------------------
# Optional TA-Lib import (guarded)
# ---------------------------------------------------------------------------
try:
    import talib as _talib  # type: ignore

    _TALIB_AVAILABLE = True
except ImportError:
    _talib = None  # type: ignore
    _TALIB_AVAILABLE = False
    logger.warning(
        "TA-Lib not installed; using pandas/numpy fallbacks. "
        "Results may differ slightly from TA-Lib."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _col(df: pd.DataFrame, name: str) -> str:
    """Resolve a column name case-insensitively (freqtrade uses lowercase OHLCV;
    the original V2 port used Capitalised names). Tries exact, lowercase,
    capitalised, then any case-insensitive match.
    """
    if name in df.columns:
        return name
    low = name.lower()
    if low in df.columns:
        return low
    cap = name.capitalize()
    if cap in df.columns:
        return cap
    for col in df.columns:
        if col.lower() == low:
            return col
    raise KeyError(
        f"Column {name!r} not in df.columns: {list(df.columns)}"
    )


def _atr_numpy(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """ATR via pandas (Wilder-ish; rolling mean of true range)."""
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
    """Wilder-smoothed RSI via pandas."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _bbands_numpy(
    close: pd.Series, period: int = 20, nbdev: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands via pandas; returns (upper, middle, lower)."""
    middle = close.rolling(window=period, min_periods=1).mean()
    std = close.rolling(window=period, min_periods=1).std(ddof=0)
    upper = middle + nbdev * std
    lower = middle - nbdev * std
    return upper, middle, lower


def _stddev_numpy(close: pd.Series, period: int = 20) -> pd.Series:
    """Population stddev (ddof=0) to match TA-Lib STDDEV."""
    return close.rolling(window=period, min_periods=1).std(ddof=0)


def _ht_dcperiod_numpy(close: pd.Series) -> pd.Series:
    """Approximate dominant-cycle period via autocorrelation. Returns
    a constant Series so callers can use ``.mean()`` identically to TA-Lib.
    """
    best_lag = 20
    best_corr = 0.0
    values = close.values
    if len(values) >= 42:
        for lag in range(2, 41):
            corr = float(np.corrcoef(values[lag:], values[:-lag])[0, 1])
            if not np.isnan(corr) and abs(corr) > best_corr:
                best_corr = abs(corr)
                best_lag = lag
    return pd.Series(float(best_lag), index=close.index)


# ===========================================================================
# GridCalculator
# ===========================================================================
class GridCalculator:
    """Stateless calculator for the unified grid engine.

    All methods are ``@staticmethod``. Pass a ``GridConfig`` and (when
    needed) a candle ``DataFrame``; the calculator never owns state.
    """

    # -----------------------------------------------------------------
    # Level generation
    # -----------------------------------------------------------------
    @staticmethod
    def generate_levels(
        config: GridConfig,
        midprice: float,
        df: Optional[pd.DataFrame] = None,
    ) -> List[float]:
        """Build a sorted list of grid price levels.

        Routing:
          1. If ``config.auto_set_grid`` -> derive ``grid_range``/``grid_distance``
             from indicators (requires ``df``).
          2. If ``config.auto_adjust`` or ``config.log_scale`` -> log-spaced
             levels across df's High/Low (requires ``df``).
          3. Otherwise -> linear levels of width ``grid_distance`` centred on
             ``midprice``, total span ``grid_range``.
        """
        if config.auto_set_grid:
            if df is None:
                raise ValueError(
                    "generate_levels: 'df' must be supplied when "
                    "config.auto_set_grid=True"
                )
            config = GridCalculator._auto_set_grid_parameters(df, config)

        use_log = config.auto_adjust or config.log_scale

        if use_log:
            if df is None:
                raise ValueError(
                    "generate_levels: 'df' must be supplied when "
                    "config.auto_adjust=True or config.log_scale=True"
                )
            return GridCalculator._generate_log_levels(
                midprice=midprice,
                grid_distance=config.grid_distance,
                grid_range=config.grid_range,
                df=df,
            )

        return GridCalculator._generate_linear_levels(
            midprice=midprice,
            grid_distance=config.grid_distance,
            grid_range=config.grid_range,
        )

    @staticmethod
    def _generate_linear_levels(
        midprice: float, grid_distance: float, grid_range: float
    ) -> List[float]:
        """Linear-spaced levels around midprice. Span = grid_range, step = grid_distance."""
        if not math.isfinite(midprice):
            raise ValueError(f"_generate_linear_levels: midprice must be finite, got {midprice!r}")
        if not math.isfinite(grid_distance) or grid_distance <= 0:
            raise ValueError(
                f"_generate_linear_levels: grid_distance must be a positive finite number, got {grid_distance!r}"
            )
        if not math.isfinite(grid_range) or grid_range <= 0:
            raise ValueError(
                f"_generate_linear_levels: grid_range must be a positive finite number, got {grid_range!r}"
            )
        start = midprice - grid_range / 2.0
        end = midprice + grid_range / 2.0
        # np.arange is end-exclusive; round to 12 decimals to dodge FP drift
        raw = np.arange(start, end, grid_distance)
        levels = sorted(float(round(x, 12)) for x in raw)
        if not levels:
            logger.warning(
                "_generate_linear_levels: produced 0 levels "
                "(start=%.6f end=%.6f step=%.6f)",
                start, end, grid_distance,
            )
        return levels

    @staticmethod
    def _generate_log_levels(
        midprice: float,
        grid_distance: float,
        grid_range: float,
        df: Optional[pd.DataFrame] = None,
        num_levels: int = 10,
    ) -> List[float]:
        """Log-spaced levels across df's full High/Low range.

        ``midprice``, ``grid_distance``, and ``grid_range`` are accepted for
        signature uniformity with the linear path but are not used directly:
        log mode derives bounds purely from historical data.
        """
        if df is None:
            raise ValueError("_generate_log_levels: df is required for log mode")
        high_col = _col(df, "High")
        low_col = _col(df, "Low")
        min_price = float(df[low_col].min())
        max_price = float(df[high_col].max())
        if min_price <= 0:
            logger.warning(
                "_generate_log_levels: min_price=%.6f <= 0; clamping to 1e-8",
                min_price,
            )
            min_price = 1e-8
        if max_price <= min_price:
            raise ValueError(
                f"_generate_log_levels: max_price ({max_price!r}) <= min_price "
                f"({min_price!r}); cannot build a multi-level grid. Check df['High']/df['Low']."
            )
        levels = np.logspace(
            math.log10(min_price), math.log10(max_price), num=num_levels
        )
        return sorted(float(x) for x in levels)

    # -----------------------------------------------------------------
    # Midprice
    # -----------------------------------------------------------------
    @staticmethod
    def calculate_midprice(df: pd.DataFrame, config: GridConfig) -> float:
        """Compute the midprice at the most-recent candle of ``df``.

        Method is selected by ``config.method``; window by ``config.period``.
        Falls back to ``df['close'].iloc[0]`` on any error (matches V2 behaviour).
        """
        if df is None or len(df) == 0:
            raise ValueError("calculate_midprice: df is empty")

        method = config.method
        period = config.period
        current_step = len(df) - 1

        try:
            close_col = _col(df, "Close")
            if method == "market_price":
                if current_step < period:
                    raise ValueError(
                        f"current_step ({current_step}) < period ({period}); "
                        "cannot use 'market_price' method."
                    )
                midprice = float(df[close_col].iloc[period - 1])

            elif method == "moving_average":
                midprice = float(
                    df[close_col]
                    .rolling(window=period, min_periods=1)
                    .mean()
                    .iloc[current_step]
                )

            elif method == "mid_high_low":
                high_col = _col(df, "High")
                low_col = _col(df, "Low")
                roll_high = float(
                    df[high_col]
                    .rolling(window=period, min_periods=1)
                    .max()
                    .iloc[current_step]
                )
                roll_low = float(
                    df[low_col]
                    .rolling(window=period, min_periods=1)
                    .min()
                    .iloc[current_step]
                )
                midprice = (roll_high + roll_low) / 2.0

            elif method == "vwap":
                volume_col = _col(df, "Volume")
                price = df[close_col]
                volume = df[volume_col]
                pv_sum = float(
                    (price * volume)
                    .rolling(window=period, min_periods=1)
                    .sum()
                    .iloc[current_step]
                )
                v_sum = float(
                    volume.rolling(window=period, min_periods=1)
                    .sum()
                    .iloc[current_step]
                )
                if v_sum == 0.0:
                    raise ValueError(
                        f"VWAP volume sum is zero at current_step={current_step}"
                    )
                midprice = pv_sum / v_sum

            else:
                # GridConfig.validate() should have caught this, but defence-in-depth
                raise ValueError(
                    f"Unknown midprice method {method!r}. Choose from "
                    "'market_price', 'moving_average', 'mid_high_low', 'vwap'."
                )

            if not math.isfinite(midprice):
                raise ValueError(
                    f"Midprice is not finite ({midprice}); check data and config."
                )

        except Exception as exc:
            logger.error(
                "calculate_midprice failed: %s — falling back to df['close'].iloc[0]",
                exc,
            )
            midprice = float(df[_col(df, "Close")].iloc[0])

        return midprice

    # -----------------------------------------------------------------
    # Auto-tuning (period + grid params from indicators)
    # -----------------------------------------------------------------
    @staticmethod
    def _auto_set_period(df: pd.DataFrame, config: GridConfig) -> int:
        """Derive a dominant-cycle period from df's close prices.

        Uses TA-Lib HT_DCPERIOD when available; numpy autocorrelation
        fallback otherwise. Always returns ``max(3, ...)`` to avoid
        degenerate small periods. Returns ``config.period`` unchanged on
        failure.
        """
        try:
            close = df[_col(df, "Close")]
        except Exception as exc:
            logger.warning("_auto_set_period: cannot read Close column (%s); using config.period", exc)
            return config.period

        if _TALIB_AVAILABLE:
            cycle_series = _talib.HT_DCPERIOD(close)
        else:
            cycle_series = _ht_dcperiod_numpy(close)
        valid = cycle_series.dropna()
        raw_mean = float(valid.mean()) if not valid.empty else float(config.period)
        period = max(3, int(raw_mean))
        logger.info("_auto_set_period: derived period=%d", period)
        return period

    @staticmethod
    def _auto_set_grid_parameters(
        df: pd.DataFrame, config: GridConfig
    ) -> GridConfig:
        """Derive ``grid_range`` and ``grid_distance`` from indicators.

        Returns a NEW ``GridConfig`` (immutable update via ``dataclasses.replace``)
        with auto-derived values. If the derived values would fail validation,
        the original config is returned unchanged with a warning.
        """
        try:
            high_col = _col(df, "High")
            low_col = _col(df, "Low")
            close_col = _col(df, "Close")
        except Exception as exc:
            logger.warning(
                "_auto_set_grid_parameters: missing OHLC column (%s); "
                "returning config unchanged", exc,
            )
            return config

        period = config.period
        if config.auto_set_period:
            period = GridCalculator._auto_set_period(df, config)

        data = df.copy()

        historical_high = float(
            data[high_col].rolling(window=period, min_periods=1).max().iloc[-1]
        )
        historical_low = float(
            data[low_col].rolling(window=period, min_periods=1).min().iloc[-1]
        )

        grid_range = historical_high - historical_low

        safe_high = max(historical_high, 1e-10)
        safe_low = max(historical_low, 1e-10)
        log_high = np.log10(safe_high)
        log_low = np.log10(safe_low)
        grid_distance_log = (log_high - log_low) / 10.0

        indicator_values: List[float] = []
        if config.selected_indicators:
            close = data[close_col]
            high = data[high_col]
            low = data[low_col]

            si = config.selected_indicators

            if "ATR" in si:
                series = (
                    _talib.ATR(high, low, close, timeperiod=period)
                    if _TALIB_AVAILABLE
                    else _atr_numpy(high, low, close, period)
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

            if "RSI" in si:
                series = (
                    _talib.RSI(close, timeperiod=period)
                    if _TALIB_AVAILABLE
                    else _rsi_numpy(close, period=period)
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

            if "CyclePeriod" in si:
                series = (
                    _talib.HT_DCPERIOD(close)
                    if _TALIB_AVAILABLE
                    else _ht_dcperiod_numpy(close)
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

            if "CyclePhase" in si:
                if _TALIB_AVAILABLE:
                    series = _talib.HT_DCPHASE(close)
                    valid = series.dropna()
                    if not valid.empty:
                        indicator_values.append(float(valid.mean()))
                else:
                    logger.warning(
                        "_auto_set_grid_parameters: 'CyclePhase' requires TA-Lib; skipping."
                    )

            if "TrendMode" in si:
                if _TALIB_AVAILABLE:
                    series = _talib.HT_TRENDMODE(close)
                    valid = series.dropna()
                    if not valid.empty:
                        indicator_values.append(float(valid.mean()))
                else:
                    logger.warning(
                        "_auto_set_grid_parameters: 'TrendMode' requires TA-Lib; skipping."
                    )

            if "BollingerBands" in si:
                if _TALIB_AVAILABLE:
                    upper, middle, lower = _talib.BBANDS(
                        close, timeperiod=period, nbdevup=2, nbdevdn=2
                    )
                else:
                    upper, middle, lower = _bbands_numpy(close, period=period, nbdev=2.0)
                mid_mean = float(middle.mean())
                if mid_mean != 0:
                    indicator_values.append(
                        (float(upper.mean()) - float(lower.mean())) / mid_mean
                    )

            if "SMA" in si:
                series = (
                    _talib.SMA(close, timeperiod=period)
                    if _TALIB_AVAILABLE
                    else close.rolling(window=period, min_periods=1).mean()
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

            if "EMA" in si:
                series = (
                    _talib.EMA(close, timeperiod=period)
                    if _TALIB_AVAILABLE
                    else close.ewm(span=period, adjust=False).mean()
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

            if "StdDev" in si:
                series = (
                    _talib.STDDEV(close, timeperiod=period, nbdev=1)
                    if _TALIB_AVAILABLE
                    else _stddev_numpy(close, period=period)
                )
                valid = series.dropna()
                if not valid.empty:
                    indicator_values.append(float(valid.mean()))

        if indicator_values:
            combined = float(np.mean(indicator_values))
            scale = 1.0 + combined / 100.0
            grid_range *= scale
            grid_distance_log *= scale

        result_distance = float(10.0 ** grid_distance_log)
        result_range = float(grid_range)

        logger.info(
            "_auto_set_grid_parameters: period=%d  grid_range=%.6f  grid_distance=%.6f",
            period, result_range, result_distance,
        )

        try:
            return dataclasses.replace(
                config,
                grid_range=result_range,
                grid_distance=result_distance,
                period=period,
            )
        except ValueError as exc:
            logger.warning(
                "_auto_set_grid_parameters: derived values failed validation "
                "(grid_range=%.6f grid_distance=%.6f period=%d): %s — "
                "returning original config unchanged",
                result_range, result_distance, period, exc,
            )
            return config

    # -----------------------------------------------------------------
    # Crossing detection (renamed from V1: drop ``get_`` prefix)
    # -----------------------------------------------------------------
    @staticmethod
    def levels_crossed_down(
        from_price: float,
        to_price: float,
        levels: List[float],
    ) -> List[float]:
        """Levels crossed downward (buy triggers): ``to_price <= level < from_price``.

        Returns levels in *descending* order (deepest cross first) so callers
        process the most-distant trigger first.
        """
        if from_price <= to_price:
            return []
        crossed = [lv for lv in levels if to_price <= lv < from_price]
        return sorted(crossed, reverse=True)

    @staticmethod
    def levels_crossed_up(
        from_price: float,
        to_price: float,
        levels: List[float],
        filled: Optional[Set[float]] = None,
    ) -> List[float]:
        """Filled levels crossed upward (sell triggers).

        ``from_price <= level < to_price`` AND ``level in filled``.
        Returns levels in *ascending* order. ``filled`` defaults to empty set
        (no levels considered filled => no sell triggers).
        """
        if to_price <= from_price:
            return []
        if filled is None:
            return []
        crossed = [
            lv for lv in levels if from_price <= lv < to_price and lv in filled
        ]
        return sorted(crossed)

    @staticmethod
    def nearest_level(price: float, levels: List[float]) -> Optional[float]:
        """Closest level to ``price`` by absolute distance, or ``None`` if empty."""
        if not levels:
            return None
        return min(levels, key=lambda lv: abs(lv - price))

    @staticmethod
    def level_below(price: float, levels: List[float]) -> Optional[float]:
        """Highest level strictly below ``price``, or ``None``."""
        below = [lv for lv in levels if lv < price]
        return max(below) if below else None

    @staticmethod
    def level_above(price: float, levels: List[float]) -> Optional[float]:
        """Lowest level strictly above ``price``, or ``None``."""
        above = [lv for lv in levels if lv > price]
        return min(above) if above else None

    # -----------------------------------------------------------------
    # Allocation (returns notional stake in quote currency, not share count)
    # -----------------------------------------------------------------
    @staticmethod
    def calculate_allocation(
        config: GridConfig,
        trade_value: float,
        current_price: float,
        atr: Optional[float] = None,
        available: Optional[float] = None,
    ) -> float:
        """Compute the actual stake (notional in quote currency) for a trade.

        Dispatches on ``config.allocation_strategy``:

        * ``"fixed_pct"``        -> ``allocation_param`` fraction of
          ``available`` (or ``trade_value`` if ``available`` is ``None``).
        * ``"fixed_shares"``     -> ``allocation_param`` shares converted to
          notional via ``current_price``.
        * ``"dynamic_grid"``     -> ``trade_value * allocation_param`` (the
          per-level scaling lives at the module layer where the level index is
          known; ``allocation_param`` lets the optimizer tune the magnitude).
        * ``"proportional"``     -> ``trade_value * allocation_param`` (same
          rationale; ``remaining_levels`` is per-call state owned by the
          module).
        * ``"volatility"``       -> ``trade_value`` reduced by 50 % when
          ``atr / current_price > config.volatility_threshold`` (full
          ``trade_value`` otherwise).

        ``available`` (when supplied) is a hard upper bound on the returned
        stake. The result is always non-negative.

        Note: the dispatcher returns a *notional* stake (float) so it
        plugs cleanly into freqtrade's ``adjust_position`` API. The V2
        share-count version (``allocate_shares``) is preserved verbatim in
        ``grid_calculator_legacy.py`` for any caller that still needs share
        counts.
        """
        if not math.isfinite(current_price) or current_price <= 0:
            logger.error(
                "calculate_allocation: current_price must be > 0, got %r", current_price
            )
            return 0.0

        strategy = config.allocation_strategy
        param = config.allocation_param

        if strategy == "fixed_pct":
            budget = available if available is not None else trade_value
            stake = float(budget) * float(param)

        elif strategy == "fixed_shares":
            stake = float(param) * float(current_price)

        elif strategy == "dynamic_grid":
            stake = float(trade_value) * float(param)

        elif strategy == "proportional":
            stake = float(trade_value) * float(param)

        elif strategy == "volatility":
            if atr is None or not math.isfinite(atr) or atr < 0:
                logger.warning(
                    "calculate_allocation: 'volatility' strategy requires a "
                    "finite non-negative atr; got %r — using trade_value as-is",
                    atr,
                )
                stake = float(trade_value)
            else:
                normalised_vol = atr / current_price
                if normalised_vol > config.volatility_threshold:
                    stake = float(trade_value) * 0.5
                    logger.debug(
                        "calculate_allocation: high vol=%.4f > threshold=%.4f; "
                        "halving stake %.2f -> %.2f",
                        normalised_vol, config.volatility_threshold,
                        trade_value, stake,
                    )
                else:
                    stake = float(trade_value)

        else:
            # GridConfig.validate() should have caught this
            raise ValueError(
                f"calculate_allocation: unknown allocation_strategy {strategy!r}. "
                f"Valid: 'fixed_pct', 'fixed_shares', 'dynamic_grid', "
                f"'proportional', 'volatility'."
            )

        if available is not None:
            stake = min(stake, float(available))

        return max(0.0, float(stake))

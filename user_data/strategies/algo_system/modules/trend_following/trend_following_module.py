"""
modules/trend_following/trend_following_module.py
=================================================
TrendFollowingModule — the second concrete IAlgoModule in the LATS system.

Unlike GridTradingModule, which trades symmetric mean-reversion grids, this
module trades WITH the prevailing trend.  It enters on momentum confirmation
and exits when the trend reverses or the position becomes overbought.

Architecture
------------
- Stateless indicator computation in ``populate_indicators``.
- Last candle evaluation in ``generate_entry_signal`` / ``generate_exit_signal``.
- Per-pair dict ``_pair_state`` tracks a small amount of mutable bookkeeping
  (last entry price, consecutive signal counts) without requiring heavy state
  objects.

Operating modes
---------------
The module supports two top-level modes controlled by the ``mode`` config key:

``"trade"`` (default)
    Emits ``enter_long=True`` when entry conditions are met.  The orchestrator
    will forward the signal to freqtrade which opens a real position.

``"alert"``
    Evaluates the same conditions but, instead of entering a trade, emits a
    structured alert through :class:`~observability.metrics_collector.MetricsCollector`
    and returns ``enter_long=False``.  Useful for paper-trading validation or
    signal monitoring without capital commitment.

Entry conditions (all must be True)
-------------------------------------
1. ``_tf_ema_fast > _tf_ema_slow``  — uptrend confirmed by EMA crossover.
2. ``rsi_entry_min < _tf_rsi < rsi_entry_max``  — momentum present, not overbought.
3. ``close > _tf_sma200``  — above long-term trend (optional; ``require_above_sma200``).
4. ``_tf_atr / close > atr_min_pct``  — sufficient volatility to justify entry.

Exit conditions (any one triggers exit)
-----------------------------------------
1. ``_tf_ema_fast < _tf_ema_slow``  — trend has reversed.
2. ``_tf_rsi > rsi_exit_overbought``  — price is overbought.
3. ``close < _tf_ema_slow * (1 - trail_stop_pct)``  — trailing stop below EMA slow.

Indicator columns added (prefixed ``_tf_``)
-------------------------------------------
- ``_tf_ema_fast``   — EMA(close, fast_period)
- ``_tf_ema_slow``   — EMA(close, slow_period)
- ``_tf_rsi``        — RSI(close, rsi_period)
- ``_tf_sma200``     — SMA(close, 200)  [optional]
- ``_tf_atr``        — ATR(high, low, close, atr_period)

All indicators attempt ``talib`` first and fall back to a pure-pandas
implementation if ``talib`` is unavailable or raises.

Config keys (read from ``shared_state.get(module_id, "config")``)
------------------------------------------------------------------
mode                  str   "trade" | "alert"         (default "trade")
fast_period           int   EMA fast window           (default 12)
slow_period           int   EMA slow window           (default 26)
rsi_period            int   RSI window                (default 14)
atr_period            int   ATR window                (default 14)
rsi_entry_min         float lower RSI bound for entry (default 45.0)
rsi_entry_max         float upper RSI bound for entry (default 70.0)
rsi_exit_overbought   float RSI threshold for exit    (default 75.0)
require_above_sma200  bool  gate entry on SMA200      (default True)
atr_min_pct           float minimum ATR/price ratio   (default 0.005)
trail_stop_pct        float trailing stop fraction    (default 0.03)
alert_channel         str   label for alert messages  (default "lats_alerts")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional

import numpy as np
import pandas as pd
from pandas import DataFrame, Series

from ...base.ialgo_module import IAlgoModule, ModuleCapability, ModuleSignal
from ...base.module_context import ModuleContext
from ...observability.metrics_collector import MetricsCollector

if TYPE_CHECKING:
    pass


logger = logging.getLogger("algo_system.trend_following_module")

# ---------------------------------------------------------------------------
# Private indicator helpers (pure-pandas fallbacks)
# ---------------------------------------------------------------------------


def _ema_pandas(series: Series, period: int) -> Series:
    """Exponential moving average using pandas ewm (talib-equivalent)."""
    return series.ewm(span=period, adjust=False).mean()  # type: ignore[return-value]


def _rsi_pandas(series: Series, period: int) -> Series:
    """
    Wilder's smoothed RSI using pandas.

    Uses EMA with ``alpha = 1 / period`` (equivalent to talib's RSI).
    Returns a Series of the same length; first ``period`` values are NaN.
    """
    delta: Series = series.diff()  # type: ignore[assignment]
    gain: Series = delta.clip(lower=0.0)  # type: ignore[assignment]
    loss: Series = -delta.clip(upper=0.0)  # type: ignore[operator]

    alpha = 1.0 / period
    avg_gain: Series = gain.ewm(alpha=alpha, adjust=False).mean()  # type: ignore[assignment]
    avg_loss: Series = loss.ewm(alpha=alpha, adjust=False).mean()  # type: ignore[assignment]

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_pandas(high: Series, low: Series, close: Series, period: int) -> Series:
    """Average True Range using pandas (Wilder smoothing to match talib)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing: alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _sma_pandas(series: Series, period: int) -> Series:
    """Simple moving average using pandas rolling."""
    return series.rolling(window=period, min_periods=period).mean()


# ---------------------------------------------------------------------------
# TrendFollowingModule
# ---------------------------------------------------------------------------


class TrendFollowingModule(IAlgoModule):
    """
    Momentum-trend-following algorithm module for the LATS system.

    Operates in one of two modes — "trade" (emits real entry signals) or
    "alert" (logs structured alerts without entering positions).  See module
    docstring for full entry/exit logic and config reference.

    Class attributes
    ----------------
    module_id : str
        Unique identifier used as a SharedState key, log prefix, and
        entry/exit tag prefix.
    version : str
        Semantic version.  Increment when the indicator set or signal logic
        changes in a backward-incompatible way.

    Capability flags
    ----------------
    ``supports_position_adjust`` is ``UNSUPPORTED`` — this module does not use
    ``adjust_position``; position sizing is delegated to the orchestrator.
    ``supports_short`` is ``UNSUPPORTED`` — only long-side trend-following.
    """

    # ------------------------------------------------------------------
    # Class-level identity
    # ------------------------------------------------------------------

    module_id: ClassVar[str] = "trend_following_v1"
    version: ClassVar[str] = "1.0.0"

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------

    supports_backtest: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_paper: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_live: ClassVar[ModuleCapability] = ModuleCapability.SUPPORTED
    supports_hyperopt: ClassVar[ModuleCapability] = ModuleCapability.PARTIAL
    supports_short: ClassVar[ModuleCapability] = ModuleCapability.UNSUPPORTED
    supports_position_adjust: ClassVar[ModuleCapability] = ModuleCapability.UNSUPPORTED

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._config: Dict[str, Any] = {}
        # per-pair mutable bookkeeping: {"entry_price": float | None, "in_position": bool}
        self._pair_state: Dict[str, Dict[str, Any]] = {}
        self._metrics: Optional[MetricsCollector] = None
        self._logger = logging.getLogger(f"algo_system.{self.module_id}")

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def initialize(self, context: ModuleContext) -> None:
        """
        Validate and cache module config from ``SharedState``.

        Config is read from ``shared_state.get(module_id, "config")``.  If not
        found, all defaults are applied.  A ``ValueError`` is raised if any
        numeric constraint is violated so that misconfiguration is caught early.

        Parameters
        ----------
        context:
            The ``ModuleContext`` provided by the orchestrator.

        Raises
        ------
        ValueError
            If ``mode`` is not ``"trade"`` or ``"alert"``, if ``fast_period >=
            slow_period``, or if any period is less than 1.
        """
        # shared_state.get() returns a ModuleStateEntry TypedDict (with keys
        # module_id, pair, updated_timestamp, data) or None.  The actual config
        # payload lives under the "data" key.
        _entry = context.shared_state.get(self.module_id, "config")
        raw_cfg: Dict[str, Any] = (_entry["data"] if _entry is not None else {})

        mode = str(raw_cfg.get("mode", "trade"))
        if mode not in ("trade", "alert"):
            raise ValueError(
                f"TrendFollowingModule: 'mode' must be 'trade' or 'alert', got '{mode}'"
            )

        fast_period = int(raw_cfg.get("fast_period", 12))
        slow_period = int(raw_cfg.get("slow_period", 26))
        rsi_period = int(raw_cfg.get("rsi_period", 14))
        atr_period = int(raw_cfg.get("atr_period", 14))

        if fast_period < 1:
            raise ValueError(f"fast_period must be >= 1, got {fast_period}")
        if slow_period < 1:
            raise ValueError(f"slow_period must be >= 1, got {slow_period}")
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be < slow_period ({slow_period})"
            )
        if rsi_period < 1:
            raise ValueError(f"rsi_period must be >= 1, got {rsi_period}")
        if atr_period < 1:
            raise ValueError(f"atr_period must be >= 1, got {atr_period}")

        rsi_entry_min = float(raw_cfg.get("rsi_entry_min", 45.0))
        rsi_entry_max = float(raw_cfg.get("rsi_entry_max", 70.0))
        rsi_exit_overbought = float(raw_cfg.get("rsi_exit_overbought", 75.0))
        require_above_sma200 = bool(raw_cfg.get("require_above_sma200", True))
        atr_min_pct = float(raw_cfg.get("atr_min_pct", 0.005))
        trail_stop_pct = float(raw_cfg.get("trail_stop_pct", 0.03))
        alert_channel = str(raw_cfg.get("alert_channel", "lats_alerts"))

        self._config = {
            "mode": mode,
            "fast_period": fast_period,
            "slow_period": slow_period,
            "rsi_period": rsi_period,
            "atr_period": atr_period,
            "rsi_entry_min": rsi_entry_min,
            "rsi_entry_max": rsi_entry_max,
            "rsi_exit_overbought": rsi_exit_overbought,
            "require_above_sma200": require_above_sma200,
            "atr_min_pct": atr_min_pct,
            "trail_stop_pct": trail_stop_pct,
            "alert_channel": alert_channel,
        }

        # Build MetricsCollector backed by the DataProvider (may be None in backtest).
        dp_inner = getattr(context.data_provider, "_dp", None)
        self._metrics = MetricsCollector(dp=dp_inner)

        self._logger.info(
            "TrendFollowingModule initialized (mode=%s, fast=%d, slow=%d)",
            mode,
            fast_period,
            slow_period,
        )

    def on_bot_start(self, context: ModuleContext) -> None:
        """
        Warm up per-pair state entries for all pairs in the whitelist.

        No heavy restoration is needed because this module's per-pair state
        is lightweight and can be rebuilt from live data on the first candle.

        Parameters
        ----------
        context:
            The ``ModuleContext`` provided by the orchestrator.
        """
        for pair in context.data_provider.current_whitelist():
            if pair not in self._pair_state:
                self._pair_state[pair] = {"entry_price": None, "in_position": False}
                self._logger.debug("Initialised state for %s", pair)

    def shutdown(self, context: ModuleContext) -> None:
        """
        Release module resources.

        Per-pair state is cleared.  The ``MetricsCollector`` holds no
        connections, so no explicit teardown is required there.

        Parameters
        ----------
        context:
            The ``ModuleContext`` provided by the orchestrator.
        """
        self._pair_state.clear()
        self._logger.info("TrendFollowingModule shut down — per-pair state cleared.")

    # ------------------------------------------------------------------
    # Per-candle: indicators
    # ------------------------------------------------------------------

    def populate_indicators(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: ModuleContext,
    ) -> DataFrame:
        """
        Attach trend-following indicator columns to *df*.

        All columns are prefixed ``_tf_`` to avoid collision with other
        modules.  Each indicator first tries ``talib``; on ``ImportError`` or
        any calculation exception the pure-pandas fallback is used instead and
        a ``DEBUG`` log message is emitted.

        Columns added
        -------------
        ``_tf_ema_fast``  — EMA(close, fast_period)
        ``_tf_ema_slow``  — EMA(close, slow_period)
        ``_tf_rsi``       — RSI(close, rsi_period)
        ``_tf_sma200``    — SMA(close, 200) — only if ``require_above_sma200``
        ``_tf_atr``       — ATR(high, low, close, atr_period)

        Parameters
        ----------
        df:
            OHLCV DataFrame; must contain columns ``open``, ``high``,
            ``low``, ``close``, ``volume``.
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        DataFrame
            The input DataFrame with indicator columns appended.
        """
        fast = self._config.get("fast_period", 12)
        slow = self._config.get("slow_period", 26)
        rsi_period = self._config.get("rsi_period", 14)
        atr_period = self._config.get("atr_period", 14)
        require_sma200 = self._config.get("require_above_sma200", True)

        close: Series = df["close"]  # type: ignore[assignment]
        high: Series = df["high"]  # type: ignore[assignment]
        low: Series = df["low"]  # type: ignore[assignment]

        # --- EMA fast ---
        try:
            import talib  # noqa: PLC0415
            df["_tf_ema_fast"] = talib.EMA(close, timeperiod=fast)
        except Exception as exc:
            self._logger.debug("talib EMA fast unavailable: %s — using pandas fallback", exc)
            df["_tf_ema_fast"] = _ema_pandas(close, fast)

        # --- EMA slow ---
        try:
            import talib  # noqa: PLC0415
            df["_tf_ema_slow"] = talib.EMA(close, timeperiod=slow)
        except Exception as exc:
            self._logger.debug("talib EMA slow unavailable: %s — using pandas fallback", exc)
            df["_tf_ema_slow"] = _ema_pandas(close, slow)

        # --- RSI ---
        try:
            import talib  # noqa: PLC0415
            df["_tf_rsi"] = talib.RSI(close, timeperiod=rsi_period)
        except Exception as exc:
            self._logger.debug("talib RSI unavailable: %s — using pandas fallback", exc)
            df["_tf_rsi"] = _rsi_pandas(close, rsi_period)

        # --- SMA 200 (optional) ---
        if require_sma200:
            try:
                import talib  # noqa: PLC0415
                df["_tf_sma200"] = talib.SMA(close, timeperiod=200)
            except Exception as exc:
                self._logger.debug("talib SMA200 unavailable: %s — using pandas fallback", exc)
                df["_tf_sma200"] = _sma_pandas(close, 200)
        else:
            # Populate with the close price so the SMA200 condition is always True
            # when the feature is disabled (price > price is always False — use NaN-safe
            # sentinel: set to 0.0 so close > 0 is always True for positive prices).
            df["_tf_sma200"] = 0.0

        # --- ATR ---
        try:
            import talib  # noqa: PLC0415
            df["_tf_atr"] = talib.ATR(high, low, close, timeperiod=atr_period)
        except Exception as exc:
            self._logger.debug("talib ATR unavailable: %s — using pandas fallback", exc)
            df["_tf_atr"] = _atr_pandas(high, low, close, atr_period)

        return df

    # ------------------------------------------------------------------
    # Per-candle: entry signal
    # ------------------------------------------------------------------

    def generate_entry_signal(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: ModuleContext,
    ) -> ModuleSignal:
        """
        Evaluate trend-following entry conditions on the last closed candle.

        In **trade mode** returns ``enter_long=True`` when all four conditions
        are met (EMA cross, RSI band, SMA200 gate, ATR floor).

        In **alert mode** the same conditions are evaluated.  When they fire,
        a structured alert is sent via :class:`MetricsCollector` and the method
        returns ``enter_long=False`` so no position is opened.

        Parameters
        ----------
        df:
            OHLCV + indicator DataFrame (output of :meth:`populate_indicators`).
        metadata:
            Freqtrade pair metadata dict; must contain ``"pair"`` key.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        ModuleSignal
            ``enter_long=True`` in trade mode when conditions fire.
            ``enter_long=False`` in alert mode (or when conditions do not fire).
            ``enter_long=None`` if the DataFrame is empty or indicators are NaN.
        """
        if df is None or df.empty:
            return ModuleSignal()

        last = df.iloc[-1]

        ema_fast = last.get("_tf_ema_fast")
        ema_slow = last.get("_tf_ema_slow")
        rsi = last.get("_tf_rsi")
        sma200 = last.get("_tf_sma200")
        atr = last.get("_tf_atr")
        close = last.get("close")

        # Guard against NaN indicators (warm-up candles)
        if any(
            v is None or (isinstance(v, float) and np.isnan(v))
            for v in (ema_fast, ema_slow, rsi, atr, close)
        ):
            return ModuleSignal()

        cfg = self._config
        rsi_entry_min: float = cfg.get("rsi_entry_min", 45.0)
        rsi_entry_max: float = cfg.get("rsi_entry_max", 70.0)
        require_sma200: bool = cfg.get("require_above_sma200", True)
        atr_min_pct: float = cfg.get("atr_min_pct", 0.005)
        mode: str = cfg.get("mode", "trade")
        alert_channel: str = cfg.get("alert_channel", "lats_alerts")

        # --- Condition evaluation ---
        cond_ema = bool(ema_fast > ema_slow)
        cond_rsi = bool(rsi_entry_min < rsi < rsi_entry_max)
        cond_sma200 = (
            bool(close > sma200)
            if (require_sma200 and sma200 is not None and not np.isnan(float(sma200)))
            else True
        )
        cond_atr = bool(close > 0 and (atr / close) > atr_min_pct)

        conditions_met = cond_ema and cond_rsi and cond_sma200 and cond_atr

        pair = metadata.get("pair", getattr(ctx, "pair", "UNKNOWN"))

        if not conditions_met:
            return ModuleSignal(enter_long=False)

        # --- Conditions fired ---
        tag = f"{self.module_id}:trend_entry"

        if mode == "alert":
            # Emit alert; do NOT open a position
            alert_msg = (
                f"[{alert_channel}] TrendFollowing ALERT — {pair}\n"
                f"EMA({cfg.get('fast_period', 12)}/{cfg.get('slow_period', 26)}) "
                f"cross UP | RSI={rsi:.1f} | ATR%={atr / close:.4f}"
            )
            if self._metrics is not None:
                self._metrics.send_alert(
                    module_id=self.module_id,
                    pair=pair,
                    message=alert_msg,
                )
            self._logger.info("Alert mode: entry conditions met for %s — alert sent, no trade.", pair)
            return ModuleSignal(
                enter_long=False,
                entry_tag=tag,
                confidence=0.9,
                metadata={
                    "mode": "alert",
                    "rsi": float(rsi),
                    "atr_pct": float(atr / close),
                    "ema_fast": float(ema_fast),
                    "ema_slow": float(ema_slow),
                },
            )

        # trade mode — enter the position
        self._logger.info(
            "Entry conditions met for %s: EMA cross=%s RSI=%.1f ATR%%=%.4f SMA200=%s",
            pair,
            cond_ema,
            rsi,
            atr / close,
            cond_sma200,
        )
        return ModuleSignal(
            enter_long=True,
            entry_tag=tag,
            confidence=0.9,
            metadata={
                "mode": "trade",
                "rsi": float(rsi),
                "atr_pct": float(atr / close),
                "ema_fast": float(ema_fast),
                "ema_slow": float(ema_slow),
            },
        )

    # ------------------------------------------------------------------
    # Per-candle: exit signal
    # ------------------------------------------------------------------

    def generate_exit_signal(
        self,
        df: DataFrame,
        metadata: dict,
        ctx: ModuleContext,
    ) -> ModuleSignal:
        """
        Evaluate trend-following exit conditions on the last closed candle.

        Any one of the following triggers ``exit_long=True``:

        1. ``_tf_ema_fast < _tf_ema_slow``  — trend reversal.
        2. ``_tf_rsi > rsi_exit_overbought`` — overbought.
        3. ``close < _tf_ema_slow * (1 - trail_stop_pct)`` — trailing stop.

        If none trigger, returns ``exit_long=False`` (explicit "hold" opinion).
        Returns an empty ``ModuleSignal`` when indicators are unavailable.

        Parameters
        ----------
        df:
            OHLCV + indicator DataFrame.
        metadata:
            Freqtrade pair metadata dict.
        ctx:
            The shared ``ModuleContext``.

        Returns
        -------
        ModuleSignal
            ``exit_long=True`` with a populated ``exit_tag`` when any exit
            condition fires; ``exit_long=False`` when conditions are clear;
            empty signal when data is unavailable.
        """
        if df is None or df.empty:
            return ModuleSignal()

        last = df.iloc[-1]

        ema_fast = last.get("_tf_ema_fast")
        ema_slow = last.get("_tf_ema_slow")
        rsi = last.get("_tf_rsi")
        close = last.get("close")

        if any(
            v is None or (isinstance(v, float) and np.isnan(v))
            for v in (ema_fast, ema_slow, rsi, close)
        ):
            return ModuleSignal()

        cfg = self._config
        rsi_exit_overbought: float = cfg.get("rsi_exit_overbought", 75.0)
        trail_stop_pct: float = cfg.get("trail_stop_pct", 0.03)

        pair = metadata.get("pair", getattr(ctx, "pair", "UNKNOWN"))

        # --- Condition 1: EMA reversal ---
        if ema_fast < ema_slow:
            self._logger.info("Exit: EMA reversal for %s (fast=%.4f slow=%.4f)", pair, ema_fast, ema_slow)
            return ModuleSignal(
                exit_long=True,
                exit_tag=f"{self.module_id}:ema_reversal",
                metadata={"reason": "ema_reversal", "ema_fast": float(ema_fast), "ema_slow": float(ema_slow)},
            )

        # --- Condition 2: RSI overbought ---
        if rsi > rsi_exit_overbought:
            self._logger.info("Exit: RSI overbought for %s (rsi=%.1f)", pair, rsi)
            return ModuleSignal(
                exit_long=True,
                exit_tag=f"{self.module_id}:rsi_overbought",
                metadata={"reason": "rsi_overbought", "rsi": float(rsi)},
            )

        # --- Condition 3: trailing stop below EMA slow ---
        trail_stop_level = float(ema_slow) * (1.0 - trail_stop_pct)
        if close < trail_stop_level:
            self._logger.info(
                "Exit: trailing stop for %s (close=%.4f stop_level=%.4f)", pair, close, trail_stop_level
            )
            return ModuleSignal(
                exit_long=True,
                exit_tag=f"{self.module_id}:trail_stop",
                metadata={
                    "reason": "trail_stop",
                    "close": float(close),
                    "ema_slow": float(ema_slow),
                    "trail_stop_level": trail_stop_level,
                },
            )

        # All conditions clear — explicit "hold" opinion
        return ModuleSignal(exit_long=False)

    # ------------------------------------------------------------------
    # Trade management hooks — no-op overrides (explicit for clarity)
    # ------------------------------------------------------------------

    def adjust_position(
        self,
        _trade: Any,
        _current_time: Any,
        _current_rate: float,
        _current_profit: float,
        _min_stake: Optional[float],
        _max_stake: float,
        _ctx: "ModuleContext",
    ) -> Optional[float]:
        """
        TrendFollowingModule does not manage position size post-entry.

        Returns ``None`` on every call — the orchestrator retains full
        control of position sizing.

        Returns
        -------
        None
            Always.
        """
        return None

    def on_order_filled(  # noqa: PLR0913
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: Any,
        ctx: ModuleContext,
    ) -> None:
        """
        No-op hook.  TrendFollowingModule does not maintain fill-price state.

        Parameters
        ----------
        pair, trade, order, current_time, ctx:
            Ignored.
        """
        _ = (pair, trade, order, current_time, ctx)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_module_state(self, pair: str) -> Dict[str, Any]:
        """
        Return a JSON-serialisable snapshot of per-pair module state.

        The returned dict always contains at minimum ``module_id`` and
        ``pair``; additional keys reflect current configuration and
        operating mode.

        Parameters
        ----------
        pair:
            Trading pair string (e.g. ``"BTC/USDT"``).

        Returns
        -------
        dict
            Snapshot of module state for *pair*.
        """
        pair_data = self._pair_state.get(pair, {})
        return {
            "module_id": self.module_id,
            "version": self.version,
            "pair": pair,
            "mode": self._config.get("mode", "trade"),
            "in_position": pair_data.get("in_position", False),
            "entry_price": pair_data.get("entry_price"),
            "config": {
                k: v for k, v in self._config.items()
                if k not in ("mode",)  # mode is already top-level
            },
        }

    def reset_module_state(self, pair: str) -> None:
        """
        Reset all per-pair internal state for *pair* to initial values.

        Called by the orchestrator when a pair is removed from the trade list
        or the module transitions to ``ModuleState.INACTIVE``.

        Parameters
        ----------
        pair:
            Trading pair string.
        """
        self._pair_state.pop(pair, None)
        self._logger.info("TrendFollowingModule state reset for %s", pair)

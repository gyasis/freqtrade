"""
modules/stock_screener/stock_screener_module.py
===============================================
StockScreenerModule — LATS algo module that screens US equities via EODHD
and yfinance, ranking them by technical momentum for the morning workflow.

This module is a *screener*, not a position manager.  Its job is to:
  1. Maintain a ranked list of top equity symbols in shared_state.
  2. Signal ``enter_long=True`` for pairs in the top-N ranked list.
  3. Signal ``exit_long=True`` when a pair drops out of the top-N.

Other modules (e.g. GridTradingModule) handle position sizing / grid logic.

Config keys (under shared_state key "<module_id>:config"):
    eodhd_api_key_env       str    env var name (default: "EODHD_API_KEY")
    symbols_file            str    path to SEC ticker JSON  (default: "symbols.json")
    min_price               float  price floor filter  (default: 5.0)
    max_price               float  price ceiling filter  (default: 500.0)
    top_n                   int    symbols to signal  (default: 10)
    cache_ttl_hours         float  hours before re-scan  (default: 20.0)
    max_symbols_scan        int    cap on symbols per run  (default: 500)
    rsi_oversold            float  RSI below this scores +1  (default: 45)
    rsi_overbought          float  RSI above this scores 0  (default: 65)
    require_macd_cross      bool   penalise bearish MACD  (default: True)
    scanner_request_limit   int    ScannerClient request limit (default 5000)
    adx_top_n               int    top N results to enrich with ADX (default 50)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple

import pandas as pd

from ...base.ialgo_module import IAlgoModule, ModuleCapability, ModuleSignal
from ...base.module_context import ModuleContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Token-bucket rate limiter for EODHD API weight budget."""

    def __init__(self, max_weight: int = 850, window_seconds: int = 60) -> None:
        self._max = max_weight
        self._window = window_seconds
        self._used: float = 0.0
        self._window_start: float = time.monotonic()

    def wait_if_needed(self, weight: int = 5) -> None:
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed >= self._window:
            self._used = 0.0
            self._window_start = now
        if self._used + weight > self._max:
            sleep_for = self._window - elapsed + 0.5
            log.debug("Rate limit: sleeping %.1fs", sleep_for)
            time.sleep(max(sleep_for, 0))
            self._used = 0.0
            self._window_start = time.monotonic()
        self._used += weight


@dataclass
class _ScreenerResult:
    ticker: str
    price: float
    rsi: Optional[float] = None
    atr: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    adx: Optional[float] = None
    score: float = 0.0
    error: Optional[str] = None
    scanner_data: Optional[Dict[str, Any]] = field(default=None)


# ---------------------------------------------------------------------------
# StockScreenerModule
# ---------------------------------------------------------------------------


class StockScreenerModule(IAlgoModule):
    """
    LATS module: screens US equities via EODHD + yfinance and ranks them
    by technical momentum.  Writes results to shared_state for other modules.
    """

    # Class-level identity (ClassVar so Pyright is happy)
    module_id: ClassVar[str] = "stock_screener"
    version: ClassVar[str] = "1.0.0"

    # Capability overrides
    supports_short: ClassVar[ModuleCapability] = ModuleCapability.UNSUPPORTED
    supports_live: ClassVar[ModuleCapability] = ModuleCapability.PARTIAL

    # shared_state keys
    RANKED_KEY: ClassVar[str] = "screener:ranked"
    LAST_RUN_KEY: ClassVar[str] = "screener:last_run_ts"

    def __init__(self) -> None:
        self._api_key: Optional[str] = None
        self._rate_limiter: _RateLimiter = _RateLimiter()
        self._cache_path: Optional[Path] = None
        self._symbols: List[str] = []
        self._ranked: List[str] = []
        self._last_run_ts: float = 0.0
        self._cfg: Dict[str, Any] = {}
        self._scoring_weights: Optional[Dict[str, float]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, context: ModuleContext) -> None:
        """Load API key, symbol list, and restore cached results."""
        raw = context.shared_state.get(self.module_id, "config") or {}
        self._cfg = raw

        # Load Bayesian-optimised scoring weights from SharedState if available
        weights_entry = context.shared_state.get(self.module_id, "scoring_weights")
        if weights_entry is not None:
            self._scoring_weights = dict(weights_entry["data"])
            log.info("%s: loaded Bayesian scoring weights from SharedState", self.module_id)
        else:
            self._scoring_weights = None

        env_var = raw.get("eodhd_api_key_env", "EODHD_API_KEY")
        self._api_key = os.environ.get(env_var)
        if not self._api_key:
            raise EnvironmentError(
                f"StockScreenerModule requires the {env_var} environment variable. "
                "Add it to your .env file or shell environment."
            )

        self._rate_limiter = _RateLimiter(
            max_weight=raw.get("rate_limit_weight", 850),
            window_seconds=60,
        )

        symbols_file = raw.get("symbols_file", "symbols.json")
        self._symbols = self._load_symbols(symbols_file)
        log.info("%s: loaded %d symbols from %s", self.module_id, len(self._symbols), symbols_file)

        cache_dir = Path(raw.get("cache_dir", ".cache/screener"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = cache_dir / "screener_cache.json"

        self._restore_cache(raw.get("cache_ttl_hours", 20.0))

    def on_bot_start(self, context: ModuleContext) -> None:
        log.info("%s started. Top symbols: %s", self.module_id, self._ranked[:5])

    def shutdown(self, context: ModuleContext) -> None:
        self._save_cache()
        log.info("%s shut down.", self.module_id)

    # ------------------------------------------------------------------
    # Per-candle pipeline
    # ------------------------------------------------------------------

    def populate_indicators(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> pd.DataFrame:
        """No columns added — screener state lives in shared_state."""
        return df

    def generate_entry_signal(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Return enter_long=True if this pair's symbol is in the top-N."""
        pair = ctx.pair
        symbol = pair.split("/")[0].upper()

        ttl = self._cfg.get("cache_ttl_hours", 20.0)
        if self._is_stale(ttl):
            self._run_scan(self._cfg)

        top_n = self._cfg.get("top_n", 10)
        top = self._ranked[:top_n]

        if symbol in top:
            rank = top.index(symbol) + 1
            return ModuleSignal(
                enter_long=True,
                entry_tag=f"{self.module_id}:rank{rank}",
                confidence=1.0 - (rank - 1) / max(top_n, 1),
                metadata={"rank": rank, "total_ranked": len(self._ranked)},
            )
        return ModuleSignal(enter_long=False)

    def generate_exit_signal(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Exit if the symbol has dropped out of top-N after re-scan."""
        pair = ctx.pair
        symbol = pair.split("/")[0].upper()
        top_n = self._cfg.get("top_n", 10)
        if self._ranked and symbol not in self._ranked[:top_n]:
            return ModuleSignal(
                exit_long=True,
                exit_tag=f"{self.module_id}:dropped_from_top{top_n}",
            )
        return ModuleSignal()

    def adjust_position(  # noqa: PLR0913
        self,
        _trade: Any,
        _current_time: Any,
        _current_rate: float,
        _current_profit: float,
        _min_stake: Optional[float],
        _max_stake: float,
        _ctx: ModuleContext,
    ) -> Optional[float]:
        """Screener does not manage position size — grid module handles this."""
        return None

    def on_order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: Any,
        ctx: ModuleContext,
    ) -> None:
        _ = (pair, trade, order, current_time, ctx)

    # ------------------------------------------------------------------
    # State introspection
    # ------------------------------------------------------------------

    def get_module_state(self, pair: str) -> Dict[str, Any]:
        return {
            "ranked_count": len(self._ranked),
            "top_10": self._ranked[:10],
            "last_scan_ts": self._last_run_ts,
            "cache_path": str(self._cache_path),
        }

    def reset_module_state(self, pair: str) -> None:
        """No per-pair state — screener is global."""
        pass

    # ------------------------------------------------------------------
    # Public helpers (for morning_run.py and tests)
    # ------------------------------------------------------------------

    def get_ranked_symbols(self, top_n: Optional[int] = None) -> List[str]:
        """Return ranked symbol list, optionally capped to top_n."""
        return list(self._ranked[:top_n] if top_n else self._ranked)

    def force_rescan(self) -> List[str]:
        """Force a fresh EODHD scan ignoring cache TTL."""
        self._run_scan(self._cfg)
        return list(self._ranked)

    # ------------------------------------------------------------------
    # Internal scan pipeline
    # ------------------------------------------------------------------

    def _run_scan(self, cfg: Dict[str, Any]) -> None:
        log.info("%s: starting equity scan…", self.module_id)

        # Try ScannerClient first (single call, all indicators)
        results = self._scan_with_scanner(cfg)

        # Enrich top-N scanner results with ADX(14) from APIClient
        if results:
            self._fetch_adx_for_top(results, cfg)  # type: ignore[attr-defined]

        if not results:
            # Fallback: old price_filter + fetch_indicators path
            min_price = cfg.get("min_price", 5.0)
            max_price = cfg.get("max_price", 500.0)
            max_scan = cfg.get("max_symbols_scan", 500)
            candidates = self._price_filter(self._symbols[:max_scan], min_price, max_price)
            log.info(
                "Price filter: %d candidates ($%.0f–$%.0f)",
                len(candidates), min_price, max_price,
            )
            results = self._fetch_indicators(candidates, cfg)

        self._ranked = self._score_and_rank(results, cfg)
        self._last_run_ts = time.time()
        self._save_cache()
        log.info("Scan done. Top 10: %s", self._ranked[:10])

    def _scan_with_scanner(self, cfg: Dict[str, Any]) -> List[_ScreenerResult]:
        """Fetch all US equity indicators in one ScannerClient call.

        Falls back gracefully to an empty list if ``eodhd.ScannerClient`` is
        unavailable, which causes ``_run_scan`` to fall back to the legacy
        APIClient path.
        """
        try:
            from eodhd import ScannerClient  # noqa: PLC0415
        except ImportError:
            log.warning("eodhd ScannerClient not available — falling back to APIClient")
            return []

        try:
            scanner = ScannerClient(self._api_key)
            df = scanner.scan_markets(
                market_type="US",
                interval="d",
                quote_currency="USD",
                request_limit=cfg.get("scanner_request_limit", 5000),
            )
        except Exception as exc:
            log.warning("ScannerClient.scan_markets failed: %s — falling back", exc)
            return []

        # df columns: symbol, close, volume, sma50, sma200, ema12, ema26,
        #             bull_market, next_action, atr14, atr14_pcnt
        min_price = cfg.get("min_price", 5.0)
        max_price = cfg.get("max_price", 500.0)
        max_scan = cfg.get("max_symbols_scan", 500)

        results: List[_ScreenerResult] = []
        for _, row in df.head(max_scan).iterrows():
            try:
                price = float(row.get("close", 0) or 0)
                if not (min_price <= price <= max_price):
                    continue
                r = _ScreenerResult(ticker=str(row["symbol"]), price=price)
                r.atr = float(row.get("atr14", 0) or 0)
                r.scanner_data = {
                    "sma50": float(row.get("sma50", 0) or 0),
                    "sma200": float(row.get("sma200", 0) or 0),
                    "ema12": float(row.get("ema12", 0) or 0),
                    "ema26": float(row.get("ema26", 0) or 0),
                    "bull_market": bool(row.get("bull_market", False)),
                    "next_action": str(row.get("next_action", "")),
                    "atr14_pcnt": float(row.get("atr14_pcnt", 0) or 0),
                }
                results.append(r)
            except Exception as exc:
                log.debug("ScannerClient row error: %s", exc)
        log.info("ScannerClient returned %d candidates after price filter", len(results))
        return results

    def _price_filter(
        self, tickers: List[str], min_p: float, max_p: float
    ) -> List[str]:
        """Batch price filter via yfinance."""
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed — skipping price filter")
            return tickers

        candidates: List[str] = []
        chunk_size = 50
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            try:
                raw = yf.download(
                    " ".join(chunk), period="2d",
                    auto_adjust=True, progress=False, threads=True,
                )
                if raw.empty:
                    continue
                close = raw["Close"]
                last_close = close.iloc[-1]
                for t in chunk:
                    try:
                        price = float(last_close[t])
                        if min_p <= price <= max_p:
                            candidates.append(t)
                    except (KeyError, TypeError, ValueError):
                        pass
            except Exception as exc:
                log.debug("yfinance chunk error: %s", exc)
        return candidates

    def _fetch_indicators(
        self, tickers: List[str], cfg: Dict[str, Any]
    ) -> List[_ScreenerResult]:
        """Fetch RSI, ATR, MACD from EODHD SDK for each candidate.

        Uses the official ``eodhd`` Python SDK (``APIClient``) instead of raw
        ``requests`` calls.  The SDK returns a pandas DataFrame; we read the
        first row of the relevant column.  The ``_RateLimiter`` is still in
        place because the SDK consumes the same API weight budget.
        """
        try:
            from eodhd import APIClient  # type: ignore[import]
        except ImportError as exc:
            log.error(
                "eodhd SDK not installed — cannot fetch indicators. "
                "Install it with: pip install eodhd  (%s)",
                exc,
            )
            return []

        api = APIClient(self._api_key)
        results: List[_ScreenerResult] = []

        for ticker in tickers:
            r = _ScreenerResult(ticker=ticker, price=0.0)
            eodhd_ticker = f"{ticker}.US"
            try:
                # RSI(14)
                self._rate_limiter.wait_if_needed(5)
                rsi_df = api.get_technical_indicator_data(
                    ticker=eodhd_ticker, function="rsi", period=14, order="d"
                )
                if rsi_df is not None and not rsi_df.empty and "rsi" in rsi_df.columns:
                    r.rsi = float(rsi_df["rsi"].iloc[0])

                # ATR(14)
                self._rate_limiter.wait_if_needed(5)
                atr_df = api.get_technical_indicator_data(
                    ticker=eodhd_ticker, function="atr", period=14, order="d"
                )
                if atr_df is not None and not atr_df.empty and "atr" in atr_df.columns:
                    r.atr = float(atr_df["atr"].iloc[0])

                # MACD(12, 26, 9)
                self._rate_limiter.wait_if_needed(5)
                macd_df = api.get_technical_indicator_data(
                    ticker=eodhd_ticker, function="macd", period=26, order="d"
                )
                if macd_df is not None and not macd_df.empty:
                    if "macd" in macd_df.columns:
                        r.macd = float(macd_df["macd"].iloc[0])
                    if "signal" in macd_df.columns:
                        r.macd_signal = float(macd_df["signal"].iloc[0])

            except Exception as exc:
                r.error = str(exc)
                log.debug("Indicator fetch error for %s: %s", ticker, exc)

            results.append(r)
        return results

    def _fetch_adx_for_top(self, results: List[_ScreenerResult], cfg: Dict[str, Any]) -> None:
        """Enrich top-N scanner results with ADX(14) from APIClient (in-place).

        Only the first ``adx_top_n`` results are enriched to stay within the
        EODHD API weight budget.  Failures are silently logged at DEBUG level
        so a single unavailable ticker does not abort the whole scan.
        """
        top_n = cfg.get("adx_top_n", 50)
        try:
            from eodhd import APIClient  # noqa: PLC0415
            api = APIClient(self._api_key)
        except ImportError:
            return
        for r in results[:top_n]:
            try:
                self._rate_limiter.wait_if_needed(5)
                adx_df = api.get_technical_indicator_data(
                    ticker=f"{r.ticker}.US",
                    function="adx",
                    period=14,
                    order="d",
                )
                if adx_df is not None and not adx_df.empty and "adx" in adx_df.columns:
                    r.adx = float(adx_df["adx"].iloc[0])
            except Exception as exc:
                log.debug("ADX fetch error for %s: %s", r.ticker, exc)

    def _w(self, key: str, default: float) -> float:
        """Get scoring weight from Bayesian-optimised weights or use hard-coded default."""
        if self._scoring_weights:
            return float(self._scoring_weights.get(key, default))
        return default

    def _score_and_rank(
        self, results: List[_ScreenerResult], cfg: Dict[str, Any]
    ) -> List[str]:
        """Score each ticker and return symbols sorted descending by score.

        Scanner path (scanner_data present) — up to 8 points:
          +2.0  bull_market == True         (w_bull_market)
          +1.0  next_action == "buy"        (w_next_action)
          +0.5  price > sma50              (w_above_sma50)
          +0.5  price > sma200             (w_above_sma200)
          +1.0  atr14_pcnt > 1.0           (w_atr_volatile)
          +1.0  ema12 > ema26              (w_ema_cross)
          +1.5  adx > 25                   (w_adx_strong — strong trend confirmed)
          +0.5  adx > 40 (additional)      (w_adx_very_strong — very strong trend)

        Weights are overridden by Bayesian-optimised values when
        ``self._scoring_weights`` is set (injected from ScreenerWeightsOptimizer).

        Legacy path (scanner_data is None) — up to 3 points:
          +1.0  RSI < rsi_oversold
          +0.5  RSI < rsi_overbought
          +1.0  MACD > MACD signal
          -0.5  MACD bearish (if require_macd_cross)
          +1.0  ATR/price > 1%
          +0.3  ATR/price <= 1%
        """
        rsi_oversold = cfg.get("rsi_oversold", 45.0)
        rsi_overbought = cfg.get("rsi_overbought", 65.0)
        require_macd = cfg.get("require_macd_cross", True)

        scored: List[Tuple[str, float]] = []
        for r in results:
            if r.error:
                continue
            s = 0.0

            if r.scanner_data is not None:
                # --- Scanner path (richer indicators) ---
                sd = r.scanner_data
                if sd.get("bull_market"):
                    s += self._w("w_bull_market", 2.0)
                if sd.get("next_action") == "buy":
                    s += self._w("w_next_action", 1.0)
                sma50 = sd.get("sma50", 0.0)
                if sma50 and r.price > sma50:
                    s += self._w("w_above_sma50", 0.5)
                sma200 = sd.get("sma200", 0.0)
                if sma200 and r.price > sma200:
                    s += self._w("w_above_sma200", 0.5)
                atr14_pcnt = sd.get("atr14_pcnt", 0.0)
                if atr14_pcnt and atr14_pcnt > 1.0:
                    s += self._w("w_atr_volatile", 1.0)
                ema12 = sd.get("ema12", 0.0)
                ema26 = sd.get("ema26", 0.0)
                if ema12 and ema26 and ema12 > ema26:
                    s += self._w("w_ema_cross", 1.0)
                # ADX scoring — uses enriched field from _fetch_adx_for_top
                if r.adx is not None:
                    if r.adx > 25:
                        s += self._w("w_adx_strong", 1.5)
                    if r.adx > 40:
                        s += self._w("w_adx_very_strong", 0.5)
            else:
                # --- Legacy path (RSI / MACD / ATR) ---
                if r.rsi is not None:
                    if r.rsi < rsi_oversold:
                        s += 1.0
                    elif r.rsi < rsi_overbought:
                        s += 0.5
                if r.macd is not None and r.macd_signal is not None:
                    if r.macd > r.macd_signal:
                        s += 1.0
                    elif require_macd:
                        s -= 0.5
                if r.atr is not None and r.atr > 0 and r.price > 0:
                    if (r.atr / r.price) > 0.01:
                        s += 1.0
                    else:
                        s += 0.3

            scored.append((r.ticker, s))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in scored]

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _is_stale(self, ttl_hours: float) -> bool:
        if not self._ranked:
            return True
        return (time.time() - self._last_run_ts) / 3600 >= ttl_hours

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.write_text(
                json.dumps({"ranked": self._ranked, "ts": self._last_run_ts})
            )
        except Exception as exc:
            log.warning("Could not save screener cache: %s", exc)

    def _restore_cache(self, ttl_hours: float) -> None:
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text())
            ts = float(data.get("ts", 0))
            age_h = (time.time() - ts) / 3600
            if age_h < ttl_hours:
                self._ranked = data.get("ranked", [])
                self._last_run_ts = ts
                log.info(
                    "Restored screener cache (%.1fh old, %d symbols)",
                    age_h, len(self._ranked),
                )
        except Exception as exc:
            log.warning("Could not restore screener cache: %s", exc)

    @staticmethod
    def _load_symbols(path: str) -> List[str]:
        p = Path(path)
        if not p.exists():
            log.warning("symbols file not found: %s", path)
            return []
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                return [v.get("ticker", k) for k, v in raw.items() if v.get("ticker")]
            if isinstance(raw, list):
                return [x if isinstance(x, str) else x.get("ticker", "") for x in raw]
        except Exception as exc:
            log.error("Failed to load symbols from %s: %s", path, exc)
        return []

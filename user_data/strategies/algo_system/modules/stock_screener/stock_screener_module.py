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
    eodhd_api_key_env   str    env var name (default: "EODHD_API_KEY")
    symbols_file        str    path to SEC ticker JSON  (default: "symbols.json")
    min_price           float  price floor filter  (default: 5.0)
    max_price           float  price ceiling filter  (default: 500.0)
    top_n               int    symbols to signal  (default: 10)
    cache_ttl_hours     float  hours before re-scan  (default: 20.0)
    max_symbols_scan    int    cap on symbols per run  (default: 500)
    rsi_oversold        float  RSI below this scores +1  (default: 45)
    rsi_overbought      float  RSI above this scores 0  (default: 65)
    require_macd_cross  bool   penalise bearish MACD  (default: True)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
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
    score: float = 0.0
    error: Optional[str] = None


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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, context: ModuleContext) -> None:
        """Load API key, symbol list, and restore cached results."""
        raw = context.shared_state.get(self.module_id, "config") or {}
        self._cfg = raw

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
        _pair: str,
        _trade: Any,
        _order: Any,
        _current_time: Any,
        _ctx: ModuleContext,
    ) -> None:
        pass

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
        min_price = cfg.get("min_price", 5.0)
        max_price = cfg.get("max_price", 500.0)
        max_scan = cfg.get("max_symbols_scan", 500)

        candidates = self._price_filter(self._symbols[:max_scan], min_price, max_price)
        log.info("Price filter: %d candidates ($%.0f–$%.0f)", len(candidates), min_price, max_price)

        results = self._fetch_indicators(candidates, cfg)
        self._ranked = self._score_and_rank(results, cfg)

        self._last_run_ts = time.time()
        self._save_cache()
        log.info("Scan done. Top 10: %s", self._ranked[:10])

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
        """Fetch RSI, ATR, MACD from EODHD for each candidate."""
        import requests

        base = "https://eodhd.com/api/technical"
        results: List[_ScreenerResult] = []

        for ticker in tickers:
            r = _ScreenerResult(ticker=ticker, price=0.0)
            try:
                def _get(fn: str, extra: Optional[dict] = None) -> list:
                    self._rate_limiter.wait_if_needed(5)
                    params: dict = {"function": fn, "api_token": self._api_key, "fmt": "json"}
                    if extra:
                        params.update(extra)
                    resp = requests.get(f"{base}/{ticker}.US", params=params, timeout=10)
                    return resp.json() if resp.ok else []

                rsi_data = _get("rsi", {"period": 14})
                if rsi_data:
                    r.rsi = float(rsi_data[0].get("rsi", 0))

                atr_data = _get("atr", {"period": 14})
                if atr_data:
                    r.atr = float(atr_data[0].get("atr", 0))

                macd_data = _get("macd", {"fast_period": 12, "slow_period": 26, "signal_period": 9})
                if macd_data:
                    r.macd = float(macd_data[0].get("macd", 0))
                    r.macd_signal = float(macd_data[0].get("signal", 0))

            except Exception as exc:
                r.error = str(exc)
                log.debug("Indicator fetch error for %s: %s", ticker, exc)

            results.append(r)
        return results

    def _score_and_rank(
        self, results: List[_ScreenerResult], cfg: Dict[str, Any]
    ) -> List[str]:
        """Score 0–3 per ticker and return sorted descending."""
        rsi_oversold = cfg.get("rsi_oversold", 45.0)
        rsi_overbought = cfg.get("rsi_overbought", 65.0)
        require_macd = cfg.get("require_macd_cross", True)

        scored: List[Tuple[str, float]] = []
        for r in results:
            if r.error:
                continue
            s = 0.0
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

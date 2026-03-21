"""
modules/crypto_screener/crypto_screener_module.py
==================================================
CryptoScreenerModule — LATS algo module that screens the top-250 crypto coins
via CoinGecko and ranks them by momentum for generating entry/exit signals.

This module is a *screener*, not a position manager.  Its job is to:
  1. Maintain a ranked list of top crypto symbols in shared_state.
  2. Signal ``enter_long=True`` for pairs whose base currency is in the top-N.
  3. Signal ``exit_long=True`` when a pair drops out of the top-N.

Config keys (under shared_state key "crypto_screener:config"):
    top_n               int    coins to signal (default 20)
    cache_ttl_hours     float  hours before re-scan (default 4.0)
    min_market_cap      float  minimum market cap in USD (default 100_000_000)
    min_volume_24h      float  minimum 24h volume in USD (default 10_000_000)
    min_price_usd       float  minimum price floor (default 0.01)
    max_price_usd       float  maximum price ceiling (default 100_000.0)
    quote_currency      str    freqtrade quote currency (default "USDT")
    vs_currency         str    CoinGecko vs_currency (default "usd")
    exclude_stablecoins list   symbols to exclude (default ["USDT","USDC","BUSD","DAI","TUSD"])
    pro_api_key_env     str    env var for CoinGecko Pro key (default "" = free tier)
    use_trending_boost  bool   apply CoinGecko trending +2 bonus (default True)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import pandas as pd

from ...base.ialgo_module import IAlgoModule, ModuleCapability, ModuleSignal
from ...base.module_context import ModuleContext

log = logging.getLogger(__name__)

# Default stablecoins to exclude from ranking
_DEFAULT_STABLECOINS: List[str] = ["USDT", "USDC", "BUSD", "DAI", "TUSD"]


# ---------------------------------------------------------------------------
# CryptoScreenerModule
# ---------------------------------------------------------------------------


class CryptoScreenerModule(IAlgoModule):
    """
    LATS module: screens the top-250 CoinGecko coins by market-cap and ranks
    them by short-term momentum.  Writes results to shared_state so other
    modules can consume the ranked list without re-fetching.
    """

    # ------------------------------------------------------------------
    # Class-level identity / capability flags
    # ------------------------------------------------------------------

    module_id: ClassVar[str] = "crypto_screener"
    version: ClassVar[str] = "1.0.0"

    supports_short: ClassVar[ModuleCapability] = ModuleCapability.UNSUPPORTED
    supports_live: ClassVar[ModuleCapability] = ModuleCapability.PARTIAL

    # shared_state keys
    RANKED_KEY: ClassVar[str] = "crypto_screener:ranked"
    LAST_RUN_KEY: ClassVar[str] = "crypto_screener:last_run_ts"

    # ------------------------------------------------------------------
    # Instance state
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._ranked: List[str] = []        # ordered list of UPPERCASE symbols
        self._last_run_ts: float = 0.0
        self._cfg: Dict[str, Any] = {}
        self._cache_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, context: ModuleContext) -> None:
        """Load configuration and restore cached results if fresh enough."""
        _entry = context.shared_state.get(self.module_id, "config")
        if _entry is None:
            raw_cfg: Dict[str, Any] = {}
        elif isinstance(_entry, dict):
            # Test mocks return the dict directly
            raw_cfg = _entry
        else:
            # Real SharedState returns a ModuleStateEntry with a .data field
            raw_cfg = _entry.data if hasattr(_entry, "data") else {}

        self._cfg = raw_cfg

        cache_dir = Path(raw_cfg.get("cache_dir", ".cache/crypto_screener"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = cache_dir / "crypto_screener_cache.json"

        self._restore_cache(raw_cfg.get("cache_ttl_hours", 4.0))
        log.info(
            "%s initialized. %d coins in ranked list.",
            self.module_id,
            len(self._ranked),
        )

    def on_bot_start(self, context: ModuleContext) -> None:
        log.info(
            "%s started. Top coins: %s",
            self.module_id,
            self._ranked[:5],
        )

    def shutdown(self, context: ModuleContext) -> None:
        self._save_cache()
        log.info("%s shut down.", self.module_id)

    # ------------------------------------------------------------------
    # Per-candle pipeline
    # ------------------------------------------------------------------

    def populate_indicators(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> pd.DataFrame:
        """No indicator columns added — screener state lives in shared_state."""
        return df

    def generate_entry_signal(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Return enter_long=True if this pair's base currency is in the top-N."""
        pair = ctx.pair
        symbol = pair.split("/")[0].upper()

        ttl = self._cfg.get("cache_ttl_hours", 4.0)
        if self._is_stale(ttl):
            self._run_scan(self._cfg)

        top_n = self._cfg.get("top_n", 20)
        top = self._ranked[:top_n]

        if symbol in top:
            rank = top.index(symbol) + 1
            confidence = 1.0 - (rank - 1) / max(top_n, 1)
            return ModuleSignal(
                enter_long=True,
                entry_tag=f"{self.module_id}:rank{rank}",
                confidence=confidence,
                metadata={"rank": rank, "total_ranked": len(self._ranked)},
            )
        return ModuleSignal(enter_long=False)

    def generate_exit_signal(
        self, df: pd.DataFrame, metadata: dict, ctx: ModuleContext
    ) -> ModuleSignal:
        """Exit if the base currency has dropped out of the top-N ranked list."""
        pair = ctx.pair
        symbol = pair.split("/")[0].upper()
        top_n = self._cfg.get("top_n", 20)

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
        """Screener does not manage position size."""
        return None

    def on_order_filled(
        self,
        pair: str,
        trade: Any,
        order: Any,
        current_time: Any,
        ctx: ModuleContext,
    ) -> None:
        pass

    # ------------------------------------------------------------------
    # State introspection
    # ------------------------------------------------------------------

    def get_module_state(self, pair: str) -> Dict[str, Any]:
        return {
            "module_id": self.module_id,
            "pair": pair,
            "ranked_count": len(self._ranked),
            "top_20": self._ranked[:20],
            "last_scan_ts": self._last_run_ts,
            "cache_path": str(self._cache_path),
        }

    def reset_module_state(self, pair: str) -> None:
        """No per-pair state — screener state is global."""
        pass

    # ------------------------------------------------------------------
    # Public helpers (for external callers and tests)
    # ------------------------------------------------------------------

    def get_ranked_pairs(self, top_n: Optional[int] = None) -> List[str]:
        """Return ranked coin pairs (e.g. ['BTC/USDT', 'ETH/USDT', ...]).

        Parameters
        ----------
        top_n:
            If given, return only the first *top_n* pairs.  If ``None``,
            return the full ranked list.
        """
        quote = self._cfg.get("quote_currency", "USDT")
        symbols = self._ranked[:top_n] if top_n is not None else self._ranked
        return [self._symbol_to_pair(s, quote) for s in symbols]

    def force_rescan(self) -> List[str]:
        """Force a fresh CoinGecko scan, ignoring the cache TTL."""
        self._run_scan(self._cfg)
        return list(self._ranked)

    # ------------------------------------------------------------------
    # Internal scan pipeline
    # ------------------------------------------------------------------

    def _run_scan(self, cfg: Dict[str, Any]) -> None:
        """Fetch the top-250 coins from CoinGecko, filter, score, and rank."""
        log.info("%s: starting crypto scan…", self.module_id)

        try:
            from pycoingecko import CoinGeckoAPI  # type: ignore[import]
        except ImportError as exc:
            log.error(
                "pycoingecko not installed — cannot scan coins. "
                "Install it with: pip install pycoingecko  (%s)",
                exc,
            )
            return

        pro_key_env = cfg.get("pro_api_key_env", "")
        pro_key = os.environ.get(pro_key_env, "") if pro_key_env else ""

        if pro_key:
            cg = CoinGeckoAPI(api_key=pro_key)
        else:
            cg = CoinGeckoAPI()

        vs_currency = cfg.get("vs_currency", "usd")

        try:
            coins = cg.get_coins_markets(
                vs_currency=vs_currency,
                order="market_cap_desc",
                per_page=250,
                page=1,
                sparkline=False,
                price_change_percentage="24h,7d",
            )
        except Exception as exc:
            log.error("%s: CoinGecko fetch failed: %s", self.module_id, exc)
            return

        # Fetch anticipatory signals once before scoring
        trending = self._fetch_trending_symbols()  # type: ignore[attr-defined]
        # hot_cats = self._fetch_hot_categories()  # logged but not used in scoring yet

        self._ranked = self._filter_and_rank(  # type: ignore[call-arg]
            coins, cfg, trending_symbols=trending
        )
        self._last_run_ts = time.time()
        self._save_cache()
        log.info("Scan done. Top 10: %s", self._ranked[:10])

    def _filter_and_rank(
        self,
        coins: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        trending_symbols: Optional[set] = None,
    ) -> List[str]:
        """Apply filters, compute momentum score, return ranked symbol list.

        Scoring (up to 7 points with trending boost enabled):
          Base scoring (0–4 points):
            +1.0  price_change_24h > 2%
            +1.0  price_change_24h > 5%  (additional strong-momentum point)
            +1.0  price_change_7d > 5%
            +1.0  volume/market_cap > 0.05

          Extended scoring (additional points):
            +1.0  volume/market_cap > 0.10  (very high relative volume)
            +1.0  price_change_24h > 10%    (very strong surge)
            +2.0  symbol in trending_symbols (anticipatory — search leads price)
        """
        min_mcap = cfg.get("min_market_cap", 100_000_000.0)
        min_vol = cfg.get("min_volume_24h", 10_000_000.0)
        min_price = cfg.get("min_price_usd", 0.01)
        max_price = cfg.get("max_price_usd", 100_000.0)
        exclude = {s.upper() for s in cfg.get("exclude_stablecoins", _DEFAULT_STABLECOINS)}
        use_trending = cfg.get("use_trending_boost", True)
        _trending = trending_symbols if trending_symbols is not None else set()

        scored: List[tuple[str, float]] = []

        for coin in coins:
            symbol = (coin.get("symbol") or "").upper()

            # --- exclusion filters ---
            if symbol in exclude:
                continue

            price = coin.get("current_price") or 0.0
            if price < min_price or price > max_price:
                continue

            mcap = coin.get("market_cap") or 0.0
            if mcap < min_mcap:
                continue

            volume = coin.get("total_volume") or 0.0
            if volume < min_vol:
                continue

            # --- base momentum scoring (0–4 points) ---
            score = 0.0
            change_24h = coin.get("price_change_percentage_24h") or 0.0
            change_7d = (
                coin.get("price_change_percentage_7d_in_currency") or 0.0
            )
            vol_to_mcap = (volume / mcap) if mcap > 0 else 0.0

            if change_24h > 2.0:
                score += 1.0
            if change_24h > 5.0:
                score += 1.0  # additional point for strong momentum
            if change_7d > 5.0:
                score += 1.0
            if vol_to_mcap > 0.05:
                score += 1.0

            # --- extended scoring (additional points) ---
            if vol_to_mcap > 0.10:
                score += 1.0  # very high relative volume
            if change_24h > 10.0:
                score += 1.0  # very strong surge

            # --- anticipatory trending boost ---
            if use_trending and symbol in _trending:
                score += 2.0

            scored.append((symbol, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in scored]

    # ------------------------------------------------------------------
    # Anticipatory signal helpers
    # ------------------------------------------------------------------

    def _fetch_trending_symbols(self) -> set:
        """Fetch top-7 trending coins from CoinGecko — leads price by hours."""
        try:
            from pycoingecko import CoinGeckoAPI  # noqa: PLC0415
            cg = CoinGeckoAPI()
            data = cg.get_search_trending()
            trending: set = set()
            for item in data.get("coins", []):
                sym = item.get("item", {}).get("symbol", "").upper()
                if sym:
                    trending.add(sym)
            log.debug("Trending coins: %s", trending)
            return trending
        except Exception as exc:
            log.debug("Trending fetch failed: %s", exc)
            return set()

    def _fetch_hot_categories(self) -> set:
        """Return category names with >5% 24h market cap change.

        CoinGecko free tier doesn't give per-coin category membership easily,
        so we flag any category with strong momentum as a signal boost
        and return the category names as metadata (for logging).
        """
        try:
            from pycoingecko import CoinGeckoAPI  # noqa: PLC0415
            cg = CoinGeckoAPI()
            cats = cg.get_coins_categories()
            hot: set = set()
            for cat in cats:
                change = cat.get("market_cap_change_24h") or 0
                if change > 5.0:
                    hot.add(cat.get("name", "").lower())
            log.debug("Hot categories: %s", hot)
            return hot
        except Exception as exc:
            log.debug("Category fetch failed: %s", exc)
            return set()

    # ------------------------------------------------------------------
    # Cache helpers
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
            log.warning("Could not save crypto screener cache: %s", exc)

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
                    "Restored crypto screener cache (%.1fh old, %d coins)",
                    age_h,
                    len(self._ranked),
                )
        except Exception as exc:
            log.warning("Could not restore crypto screener cache: %s", exc)

    @staticmethod
    def _symbol_to_pair(symbol: str, quote_currency: str = "USDT") -> str:
        """Convert a coin symbol to a freqtrade pair string.

        Example: ``_symbol_to_pair("BTC", "USDT")`` → ``"BTC/USDT"``
        """
        return f"{symbol}/{quote_currency}"

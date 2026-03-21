"""
tests/test_crypto_screener.py
==============================
Unit tests for CryptoScreenerModule.

All CoinGecko API calls are mocked — no real HTTP requests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pandas as pd

from ..modules.crypto_screener.crypto_screener_module import CryptoScreenerModule


# ---------------------------------------------------------------------------
# Test data factories
# ---------------------------------------------------------------------------


def _coin(
    symbol: str,
    price: float = 100.0,
    market_cap: float = 5_000_000_000.0,
    volume: float = 500_000_000.0,
    change_24h: float = 3.0,
    change_7d: float = 6.0,
) -> Dict[str, Any]:
    """Build a minimal CoinGecko market-data dict."""
    return {
        "id": symbol.lower(),
        "symbol": symbol.lower(),
        "current_price": price,
        "market_cap": market_cap,
        "total_volume": volume,
        "price_change_percentage_24h": change_24h,
        "price_change_percentage_7d_in_currency": change_7d,
    }


def _make_shared_state(cfg: Dict[str, Any]) -> MagicMock:
    ss = MagicMock()
    ss.get.side_effect = lambda module_id, key: cfg if key == "config" else None
    return ss


def _make_ctx(pair: str = "BTC/USDT", cfg: Dict[str, Any] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.pair = pair
    ctx.shared_state = _make_shared_state(cfg or {})
    return ctx


def _base_cfg(tmp_path: Path) -> Dict[str, Any]:
    return {
        "top_n": 20,
        "cache_ttl_hours": 4.0,
        "min_market_cap": 100_000_000.0,
        "min_volume_24h": 10_000_000.0,
        "min_price_usd": 0.01,
        "max_price_usd": 100_000.0,
        "quote_currency": "USDT",
        "vs_currency": "usd",
        "exclude_stablecoins": ["USDT", "USDC", "BUSD", "DAI", "TUSD"],
        "cache_dir": str(tmp_path / ".cache/crypto_screener"),
    }


def _make_module(tmp_path: Path, cfg: Dict[str, Any]) -> CryptoScreenerModule:
    """Create an initialized CryptoScreenerModule."""
    m = CryptoScreenerModule()
    ctx = _make_ctx(cfg=cfg)
    m.initialize(ctx)
    return m


# ---------------------------------------------------------------------------
# TestScoring
# ---------------------------------------------------------------------------


class TestScoring:
    """Verify that the momentum scoring and filtering logic is correct."""

    def test_high_momentum_wins(self, tmp_path: Path) -> None:
        """A coin with 10% 24h change should score higher than one with 1%."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        coins = [
            _coin("HIGH", change_24h=10.0, change_7d=8.0),
            _coin("LOW", change_24h=1.0, change_7d=2.0),
        ]
        ranked = m._filter_and_rank(coins, m._cfg)
        assert ranked[0] == "HIGH"
        assert ranked[1] == "LOW"

    def test_stablecoins_excluded(self, tmp_path: Path) -> None:
        """USDT, USDC etc. must never appear in the ranked output."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        coins = [
            _coin("BTC", change_24h=5.0),
            _coin("usdt", change_24h=0.1),   # symbol from CoinGecko is lowercase
            _coin("usdc", change_24h=0.1),
        ]
        ranked = m._filter_and_rank(coins, m._cfg)
        assert "USDT" not in ranked
        assert "USDC" not in ranked
        assert "BTC" in ranked

    def test_volume_filter(self, tmp_path: Path) -> None:
        """A coin below min_volume_24h must be excluded."""
        cfg = _base_cfg(tmp_path)
        cfg["min_volume_24h"] = 50_000_000.0
        m = _make_module(tmp_path, cfg)
        coins = [
            _coin("BTC", volume=500_000_000.0),
            _coin("LOWVOL", volume=1_000_000.0),  # below threshold
        ]
        ranked = m._filter_and_rank(coins, m._cfg)
        assert "LOWVOL" not in ranked
        assert "BTC" in ranked

    def test_mcap_filter(self, tmp_path: Path) -> None:
        """A coin below min_market_cap must be excluded."""
        cfg = _base_cfg(tmp_path)
        cfg["min_market_cap"] = 1_000_000_000.0
        m = _make_module(tmp_path, cfg)
        coins = [
            _coin("BTC", market_cap=500_000_000_000.0),
            _coin("MICRO", market_cap=50_000_000.0),  # below threshold
        ]
        ranked = m._filter_and_rank(coins, m._cfg)
        assert "MICRO" not in ranked
        assert "BTC" in ranked

    def test_price_bounds(self, tmp_path: Path) -> None:
        """Coins outside [min_price_usd, max_price_usd] must be excluded."""
        cfg = _base_cfg(tmp_path)
        cfg["min_price_usd"] = 1.0
        cfg["max_price_usd"] = 1_000.0
        m = _make_module(tmp_path, cfg)
        coins = [
            _coin("CHEAP", price=0.001),    # too cheap
            _coin("EXPENSIVE", price=99_999.0),  # too expensive
            _coin("OK", price=50.0),
        ]
        ranked = m._filter_and_rank(coins, m._cfg)
        assert "CHEAP" not in ranked
        assert "EXPENSIVE" not in ranked
        assert "OK" in ranked


# ---------------------------------------------------------------------------
# TestEntrySignal
# ---------------------------------------------------------------------------


class TestEntrySignal:
    """Verify generate_entry_signal behaviour."""

    def _fresh_module(
        self, tmp_path: Path, ranked: List[str], top_n: int = 20
    ) -> CryptoScreenerModule:
        cfg = _base_cfg(tmp_path)
        cfg["top_n"] = top_n
        m = _make_module(tmp_path, cfg)
        m._ranked = ranked
        m._last_run_ts = time.time()
        return m

    def test_top_coin_enters(self, tmp_path: Path) -> None:
        """BTC at rank 1 should produce enter_long=True."""
        m = self._fresh_module(tmp_path, ["BTC", "ETH", "BNB"])
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("BTC/USDT", m._cfg))
        assert sig.enter_long is True
        assert sig.metadata["rank"] == 1

    def test_outside_top_n_no_enter(self, tmp_path: Path) -> None:
        """A coin at rank 21 with top_n=20 must not enter."""
        ranked = [f"COIN{i}" for i in range(21)]  # COIN0..COIN20
        m = self._fresh_module(tmp_path, ranked, top_n=20)
        # COIN20 is at index 20 → rank 21 → outside top_n
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("COIN20/USDT", m._cfg))
        assert sig.enter_long is False

    def test_confidence_decreases_with_rank(self, tmp_path: Path) -> None:
        """Rank-1 confidence must be strictly greater than rank-5 confidence."""
        ranked = ["BTC", "ETH", "BNB", "XRP", "SOL"]
        m = self._fresh_module(tmp_path, ranked, top_n=20)
        sig1 = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("BTC/USDT", m._cfg))
        sig5 = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("SOL/USDT", m._cfg))
        assert sig1.confidence > sig5.confidence

    def test_stale_cache_triggers_rescan(self, tmp_path: Path) -> None:
        """When the cache is stale, _run_scan must be called before deciding."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = []
        m._last_run_ts = 0.0  # forces stale
        with patch.object(m, "_run_scan") as mock_scan:
            m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("BTC/USDT", m._cfg))
            mock_scan.assert_called_once()


# ---------------------------------------------------------------------------
# TestExitSignal
# ---------------------------------------------------------------------------


class TestExitSignal:
    """Verify generate_exit_signal behaviour."""

    def test_dropped_coin_exits(self, tmp_path: Path) -> None:
        """A coin no longer in the top-N ranked list should trigger exit."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["ETH", "BNB", "XRP"]
        # BTC is not in the ranked list → should exit
        sig = m.generate_exit_signal(pd.DataFrame(), {}, _make_ctx("BTC/USDT", m._cfg))
        assert sig.exit_long is True

    def test_top_coin_no_exit(self, tmp_path: Path) -> None:
        """A coin still in the top-N should not trigger an exit signal."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["BTC", "ETH", "BNB"]
        sig = m.generate_exit_signal(pd.DataFrame(), {}, _make_ctx("BTC/USDT", m._cfg))
        assert sig.exit_long is not True


# ---------------------------------------------------------------------------
# TestCache
# ---------------------------------------------------------------------------


class TestCache:
    """Verify that disk-based cache save/restore works correctly."""

    def test_saves_and_restores_cache(self, tmp_path: Path) -> None:
        """Ranked list saved to disk must be restored on next initialize()."""
        cfg = _base_cfg(tmp_path)
        m = _make_module(tmp_path, cfg)
        m._ranked = ["BTC", "ETH", "SOL"]
        m._last_run_ts = time.time() - 60  # 1 minute old — within 4h TTL
        m._save_cache()

        # Create a fresh module and verify the cache is restored
        m2 = _make_module(tmp_path, cfg)
        assert m2._ranked == ["BTC", "ETH", "SOL"]

    def test_ignores_stale_cache(self, tmp_path: Path) -> None:
        """A cache older than cache_ttl_hours must not be restored."""
        cfg = _base_cfg(tmp_path)
        cfg["cache_ttl_hours"] = 4.0
        m = _make_module(tmp_path, cfg)

        # Write a stale cache manually
        cache_dir = tmp_path / ".cache/crypto_screener"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "crypto_screener_cache.json").write_text(
            json.dumps({"ranked": ["STALE_COIN"], "ts": time.time() - 5 * 3600})
        )

        m2 = _make_module(tmp_path, cfg)
        assert m2._ranked != ["STALE_COIN"]


# ---------------------------------------------------------------------------
# TestPairHelpers
# ---------------------------------------------------------------------------


class TestPairHelpers:
    """Verify _symbol_to_pair and get_ranked_pairs helpers."""

    def test_symbol_to_pair(self, tmp_path: Path) -> None:
        """_symbol_to_pair must format 'BTC' → 'BTC/USDT'."""
        result = CryptoScreenerModule._symbol_to_pair("BTC", "USDT")
        assert result == "BTC/USDT"

    def test_get_ranked_pairs_returns_usdt_pairs(self, tmp_path: Path) -> None:
        """get_ranked_pairs must return pairs like 'BTC/USDT', 'ETH/USDT'."""
        cfg = _base_cfg(tmp_path)
        cfg["quote_currency"] = "USDT"
        m = _make_module(tmp_path, cfg)
        m._ranked = ["BTC", "ETH", "SOL"]
        pairs = m.get_ranked_pairs(top_n=3)
        assert pairs == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    def test_get_ranked_pairs_respects_top_n(self, tmp_path: Path) -> None:
        """get_ranked_pairs(top_n=2) should return only the top 2 pairs."""
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["BTC", "ETH", "SOL", "BNB"]
        pairs = m.get_ranked_pairs(top_n=2)
        assert len(pairs) == 2
        assert pairs[0] == "BTC/USDT"
        assert pairs[1] == "ETH/USDT"

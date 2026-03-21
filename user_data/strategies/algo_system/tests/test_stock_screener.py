"""
tests/test_stock_screener.py
============================
Unit tests for StockScreenerModule.

All EODHD and yfinance calls are mocked — no real HTTP requests.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ..modules.stock_screener.stock_screener_module import (
    StockScreenerModule,
    _RateLimiter,
    _ScreenerResult,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_shared_state(cfg: dict) -> MagicMock:
    ss = MagicMock()
    ss.get.side_effect = lambda module_id, key: cfg if key == "config" else None
    return ss


def _make_ctx(pair: str = "TSLA/USD", cfg: dict | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.pair = pair
    ctx.shared_state = _make_shared_state(cfg or {})
    return ctx


def _base_cfg(tmp_path: Path) -> dict:
    return {
        "eodhd_api_key_env": "EODHD_API_KEY",
        "symbols_file": str(tmp_path / "symbols.json"),
        "min_price": 10.0,
        "max_price": 300.0,
        "top_n": 5,
        "cache_ttl_hours": 24.0,
        "max_symbols_scan": 20,
        "rsi_oversold": 45.0,
        "rsi_overbought": 65.0,
        "require_macd_cross": True,
        "cache_dir": str(tmp_path / ".cache/screener"),
    }


def _make_symbols(tmp_path: Path, tickers: list[str]) -> None:
    data = {t: {"ticker": t} for t in tickers}
    (tmp_path / "symbols.json").write_text(json.dumps(data))


def _make_module(tmp_path: Path, cfg: dict, env: dict | None = None) -> StockScreenerModule:
    """Create an initialized StockScreenerModule with mocked price filter."""
    m = StockScreenerModule()
    ctx = _make_ctx(cfg=cfg)
    env_vars = {"EODHD_API_KEY": "test-key"} if env is None else env
    with patch.dict("os.environ", env_vars):
        with patch.object(m, "_price_filter", return_value=[]):
            m.initialize(ctx)
    return m


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_does_not_sleep_under_budget(self):
        rl = _RateLimiter(max_weight=850, window_seconds=60)
        start = time.monotonic()
        for _ in range(10):
            rl.wait_if_needed(5)
        assert time.monotonic() - start < 1.0

    def test_resets_after_window_expiry(self):
        rl = _RateLimiter(max_weight=10, window_seconds=1)
        rl.wait_if_needed(10)
        time.sleep(1.1)
        start = time.monotonic()
        rl.wait_if_needed(5)
        assert time.monotonic() - start < 0.5


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_raises_without_api_key(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        _make_symbols(tmp_path, ["AAPL"])
        m = StockScreenerModule()
        ctx = _make_ctx(cfg=cfg)
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(EnvironmentError, match="EODHD_API_KEY"):
                m.initialize(ctx)

    def test_loads_symbols(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        tickers = ["AAPL", "TSLA", "MSFT"]
        _make_symbols(tmp_path, tickers)
        m = _make_module(tmp_path, cfg)
        assert set(m._symbols) == set(tickers)

    def test_missing_symbols_file_gives_empty(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        # No symbols.json created
        m = _make_module(tmp_path, cfg)
        assert m._symbols == []

    def test_cache_dir_created(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        _make_symbols(tmp_path, ["AAPL"])
        _make_module(tmp_path, cfg)
        assert (tmp_path / ".cache/screener").exists()

    def test_restores_fresh_cache(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        _make_symbols(tmp_path, ["AAPL"])
        cache_dir = tmp_path / ".cache/screener"
        cache_dir.mkdir(parents=True)
        (cache_dir / "screener_cache.json").write_text(json.dumps({
            "ranked": ["TSLA", "AAPL"],
            "ts": time.time() - 3600,  # 1h old — within 24h TTL
        }))
        m = _make_module(tmp_path, cfg)
        assert m._ranked == ["TSLA", "AAPL"]

    def test_ignores_stale_cache(self, tmp_path):
        cfg = _base_cfg(tmp_path)
        _make_symbols(tmp_path, ["AAPL"])
        cache_dir = tmp_path / ".cache/screener"
        cache_dir.mkdir(parents=True)
        (cache_dir / "screener_cache.json").write_text(json.dumps({
            "ranked": ["STALE"],
            "ts": time.time() - 90_000,  # 25h — stale
        }))
        m = _make_module(tmp_path, cfg)
        assert m._ranked != ["STALE"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestScoreAndRank:
    def _r(self, ticker, rsi=None, macd=None, sig=None, atr=None, price=50.0):
        r = _ScreenerResult(ticker=ticker, price=price)
        r.rsi, r.macd, r.macd_signal, r.atr = rsi, macd, sig, atr
        return r

    def test_oversold_rsi_wins(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        results = [
            self._r("OVERSOLD", rsi=30, macd=1.0, sig=0.5, atr=1.0),
            self._r("OVERBOUGHT", rsi=80, macd=1.0, sig=0.5, atr=1.0),
        ]
        ranked = m._score_and_rank(results, m._cfg)
        assert ranked[0] == "OVERSOLD"

    def test_bullish_macd_wins(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        results = [
            self._r("BULL", rsi=50, macd=2.0, sig=1.0, atr=0.5),
            self._r("BEAR", rsi=50, macd=0.5, sig=2.0, atr=0.5),
        ]
        assert m._score_and_rank(results, m._cfg)[0] == "BULL"

    def test_errors_excluded(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        good = self._r("GOOD", rsi=40, macd=1.0, sig=0.5, atr=1.0)
        bad = _ScreenerResult(ticker="BAD", price=10.0)
        bad.error = "timeout"
        ranked = m._score_and_rank([good, bad], m._cfg)
        assert "BAD" not in ranked
        assert "GOOD" in ranked

    def test_high_atr_wins(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        results = [
            self._r("VOL", rsi=50, macd=1.0, sig=0.5, atr=5.0, price=50),
            self._r("FLAT", rsi=50, macd=1.0, sig=0.5, atr=0.1, price=50),
        ]
        assert m._score_and_rank(results, m._cfg)[0] == "VOL"


# ---------------------------------------------------------------------------
# generate_entry_signal
# ---------------------------------------------------------------------------

class TestEntrySignal:
    def _fresh_module(self, tmp_path, ranked):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ranked
        m._last_run_ts = time.time()
        return m

    def test_top_symbol_enters(self, tmp_path):
        m = self._fresh_module(tmp_path, ["TSLA", "AAPL", "MSFT", "GOOGL", "AMZN"])
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
        assert sig.enter_long is True
        assert sig.metadata["rank"] == 1

    def test_outside_top_n_false(self, tmp_path):
        m = self._fresh_module(tmp_path, ["TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("NVDA/USD", m._cfg))
        assert sig.enter_long is False  # rank 6, top_n=5

    def test_entry_tag_includes_rank(self, tmp_path):
        m = self._fresh_module(tmp_path, ["TSLA", "AAPL"])
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("AAPL/USD", m._cfg))
        assert "rank2" in sig.entry_tag

    def test_confidence_decreases_with_rank(self, tmp_path):
        m = self._fresh_module(tmp_path, ["TSLA", "AAPL", "MSFT", "GOOGL", "AMZN"])
        sig1 = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
        sig5 = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("AMZN/USD", m._cfg))
        assert sig1.confidence > sig5.confidence

    def test_stale_cache_triggers_rescan(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = []
        m._last_run_ts = 0.0
        with patch.object(m, "_run_scan") as mock_scan:
            m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
            mock_scan.assert_called_once()

    def test_pair_with_slash_parsed_correctly(self, tmp_path):
        m = self._fresh_module(tmp_path, ["AAPL", "TSLA"])
        sig = m.generate_entry_signal(pd.DataFrame(), {}, _make_ctx("AAPL/USD", m._cfg))
        assert sig.enter_long is True


# ---------------------------------------------------------------------------
# generate_exit_signal
# ---------------------------------------------------------------------------

class TestExitSignal:
    def test_dropped_symbol_exits(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["AAPL", "MSFT", "GOOGL", "AMZN", "META"]
        sig = m.generate_exit_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
        assert sig.exit_long is True

    def test_top_symbol_no_exit(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["TSLA", "AAPL", "MSFT", "GOOGL", "AMZN"]
        sig = m.generate_exit_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
        assert sig.exit_long is not True

    def test_exit_tag_mentions_top_n(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["AAPL"]
        sig = m.generate_exit_signal(pd.DataFrame(), {}, _make_ctx("TSLA/USD", m._cfg))
        assert "top5" in sig.exit_tag


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

class TestPublicHelpers:
    def test_get_ranked_returns_copy(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["A", "B", "C"]
        result = m.get_ranked_symbols()
        result.append("INJECTED")
        assert "INJECTED" not in m._ranked

    def test_get_ranked_top_n(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["A", "B", "C", "D", "E"]
        assert m.get_ranked_symbols(top_n=3) == ["A", "B", "C"]

    def test_force_rescan_calls_run_scan(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        with patch.object(m, "_run_scan") as mock_scan:
            m.force_rescan()
            mock_scan.assert_called_once()

    def test_get_module_state(self, tmp_path):
        m = _make_module(tmp_path, _base_cfg(tmp_path))
        m._ranked = ["X", "Y"]
        state = m.get_module_state("ANY/USD")
        assert state["ranked_count"] == 2
        assert state["top_10"] == ["X", "Y"]

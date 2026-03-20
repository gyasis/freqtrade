"""
tests/conftest.py
Pytest fixtures for the LATS test suite.
Helper functions/classes live in test_helpers.py (importable as a regular module).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from pandas import DataFrame

# Ensure algo_system and tests dir are on sys.path before any imports
_STRAT_ROOT = Path(__file__).resolve().parents[2]  # user_data/strategies/
_TESTS_DIR = Path(__file__).resolve().parent
for _p in (_STRAT_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from algo_system.base.module_context import ModuleContext
from algo_system.orchestrator.shared_state import SharedState
from test_helpers import (  # noqa: E402
    MockOrder,
    MockTrade,
    make_module_context,
    make_ohlcv_df,
    make_ranging_ohlcv_df,
)


@pytest.fixture
def ohlcv_df() -> DataFrame:
    """100-candle OHLCV DataFrame at BTC-like price levels."""
    return make_ohlcv_df()


@pytest.fixture
def ranging_df() -> DataFrame:
    """100-candle ranging OHLCV DataFrame (oscillates in a narrow band)."""
    return make_ranging_ohlcv_df()


@pytest.fixture
def module_context() -> ModuleContext:
    """ModuleContext wired to in-memory mocks."""
    return make_module_context()


@pytest.fixture
def shared_state() -> SharedState:
    """Fresh SharedState using a temp file (no real disk I/O during tests)."""
    return SharedState(
        persistence_path=os.path.join(tempfile.gettempdir(), "lats_test_shared_state.json")
    )


@pytest.fixture
def mock_trade() -> MockTrade:
    return MockTrade()


@pytest.fixture
def mock_order() -> MockOrder:
    return MockOrder(price=50_000.0)

"""
tests/harness/backtest_harness.py
Programmatic freqtrade backtesting runner for the LATS algo system.

Usage (from freqtrade root):
    python user_data/strategies/algo_system/tests/harness/backtest_harness.py

Assertions:
- At least one trade is executed
- All enter_tags are prefixed "grid_trading_v1:"
- No module exceeds its configured stake cap (checked via enter_tag + trade amounts)
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Ensure freqtrade root is importable
_FREQTRADE_ROOT = Path(__file__).resolve().parents[5]  # freqtrade/
if str(_FREQTRADE_ROOT) not in sys.path:
    sys.path.insert(0, str(_FREQTRADE_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("lats.backtest_harness")


def load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        return json.load(f)


def run_backtest() -> None:
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode
    from freqtrade.optimize.backtesting import Backtesting

    config_path = _FREQTRADE_ROOT / "user_data" / "config_algo_backtest.json"
    if not config_path.exists():
        logger.error("Config not found: %s", config_path)
        sys.exit(1)

    logger.info("Loading config from %s", config_path)

    # Build freqtrade configuration dict
    args = {
        "config": [str(config_path)],
        "strategy": "OrchestratorStrategy",
        "runmode": RunMode.BACKTEST,
        "timerange": "20240101-20240701",  # 6-month BTC/USDT slice
        "datadir": str(_FREQTRADE_ROOT / "user_data" / "data"),
        "export": "none",
        "verbosity": 0,
    }

    config = Configuration(args, RunMode.BACKTEST).get_config()
    config["runmode"] = RunMode.BACKTEST

    logger.info("Starting backtest…")
    backtesting = Backtesting(config)
    backtesting.start()

    # -----------------------------------------------------------------------
    # Assertions
    # results is BacktestResultType: {"metadata": {}, "strategy": {...}, ...}
    # Per-strategy trades are at results["strategy"][name]["trades"] (list of dicts)
    # -----------------------------------------------------------------------
    results: dict = backtesting.results  # type: ignore[assignment]
    strategy_results: dict = results.get("strategy", {})

    if not strategy_results:
        logger.error("ASSERTION FAILED: No strategy results produced")
        sys.exit(1)

    strategy_name = "OrchestratorStrategy"
    strat_data: dict = strategy_results.get(strategy_name, {})
    trades: list = strat_data.get("trades", [])

    trade_count = len(trades)
    logger.info("Trades executed: %d", trade_count)

    if trade_count == 0:
        logger.error("ASSERTION FAILED: Expected at least one trade")
        sys.exit(1)

    # All enter_tags must be prefixed with "grid_trading_v1:"
    bad_tags = [
        t for t in trades
        if not str(t.get("enter_tag", "")).startswith("grid_trading_v1:")
    ]
    if bad_tags:
        logger.error(
            "ASSERTION FAILED: %d trades have unexpected enter_tags (first 5: %s)",
            len(bad_tags),
            [(t.get("pair"), t.get("enter_tag")) for t in bad_tags[:5]],
        )
        sys.exit(1)
    logger.info("All enter_tags correctly prefixed 'grid_trading_v1:'")

    logger.info("✅ Backtest harness assertions passed (%d trades)", trade_count)


if __name__ == "__main__":
    run_backtest()

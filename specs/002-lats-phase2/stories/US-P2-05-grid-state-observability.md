# US-P2-05: Grid State Observability Dashboard

## Problem

When the bot is running there is no easy way to see:
- Which grid levels are currently filled (bought) vs unfilled
- How far the current price is from the nearest trigger
- How many grid cycles (buy → sell round-trips) have completed
- Whether the grid is healthy or stuck (price has left the range)

Operators must dig into log files or read raw `SharedState` JSON to understand
what the grid is doing.

## Goal

`GridTradingModule` exposes a rich status snapshot per pair, surfaced via
`MetricsCollector` and accessible through freqtrade's `status` command.

## Acceptance Criteria

1. `GridState` gains two new fields:
   - `completed_cycles: int` — incremented each time a level is bought AND
     subsequently sold (full round-trip).
   - `last_action: Optional[str]` — e.g. `"buy@48750"` or `"sell@50000"`,
     updated on every `adjust_position` action.

2. `GridTradingModule.get_module_state(pair)` returns an extended dict:
   ```python
   {
       "pair": "BTC/USDT",
       "grid_range": [47500.0, 52500.0],
       "levels": [47500, 48750, 50000, 51250, 52500],
       "filled": [48750, 50000],      # bought, waiting for price to rise
       "unfilled": [47500, 51250, 52500],
       "current_price": 49200.0,
       "nearest_buy_level": 47500.0,
       "nearest_sell_level": 48750.0,  # lowest filled level above price
       "in_range": True,
       "completed_cycles": 3,
       "last_action": "buy@48750",
   }
   ```

3. `OrchestratorStrategy` calls `MetricsCollector.send_grid_status(pair, status_dict)`
   every 24 candles (configurable).  `MetricsCollector.send_grid_status` formats
   the dict as a human-readable Telegram message and calls `dp.send_msg`.

4. A new `tests/test_grid_observability.py` verifies:
   - `completed_cycles` increments only on full buy+sell round-trips.
   - `last_action` reflects the most recent `adjust_position` outcome.
   - `get_module_state` contains all required keys.
   - Out-of-range pair shows `"in_range": False`.

## Out of Scope

- Web dashboard or REST endpoint (freqtrade's built-in API serves that)
- Historical cycle tracking (only current session)

## Technical Notes

- `send_grid_status` can be a new method on `MetricsCollector` alongside the
  existing `send_suspension_alert`, `send_circuit_breaker_alert`, etc.
- `nearest_buy_level` = lowest unfilled level below current price.
- `nearest_sell_level` = lowest filled level above current price.
- Both can be `None` if no such level exists.

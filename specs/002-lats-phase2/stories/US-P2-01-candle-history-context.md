# US-P2-01: Candle History in Module Context

## Problem

Every `IAlgoModule` method receives the full OHLCV DataFrame for the pair, but
`adjust_position` — the hot path called every candle per open trade — receives
**only the current price** (`current_rate`).  There is no canonical way for a
module to know the last N closes, the previous candle's open/high/low, or any
derived indicator value from a few candles back without querying `DataProvider`
manually.

The `prev_rate` bug (T-bugfix: stale `trade.open_rate`) was a symptom of this:
modules improvise and use whatever stale state they can find.

## Goal

`ModuleContext` provides a lightweight **candle snapshot** — the last N completed
OHLCV rows — available in every module method without DataProvider overhead.

## Acceptance Criteria

1. `ModuleContext` gains a field `recent_candles: Optional[DataFrame]` (last 20
   rows of the pair's 1h OHLCV, set by the orchestrator from the DataProvider
   before dispatching to modules).

2. `adjust_position` implementations can access `ctx.recent_candles` and read
   `ctx.recent_candles["close"].iloc[-2]` (previous close) without any additional
   calls.

3. The orchestrator populates `recent_candles` in `_make_context` using
   `dp.get_pair_dataframe(pair, timeframe).tail(20)`.

4. Unit tests pass when `recent_candles` is `None` (graceful degradation — all
   existing tests continue to work with no candle snapshot).

5. `GridTradingModule.adjust_position` is refactored to use
   `ctx.recent_candles["close"].iloc[-1]` as `prev_rate` when available, falling
   back to `_last_prices[pair]` when not.

## Out of Scope

- Multi-timeframe candles in context (separate story)
- Changing the DataProvider call cadence

## Technical Notes

- `recent_candles` must be a **copy** (not a live reference) to prevent
  modules from accidentally mutating shared state.
- Populating 20 rows on every `_make_context` call has negligible overhead
  since the DataFrame is already in memory from `populate_indicators`.

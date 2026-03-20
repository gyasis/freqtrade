# US-P2-06: Grid Exit Strategy (Range Breakout Detection)

## Problem

Once a grid is initialised it runs indefinitely.  If the price permanently
breaks out of the grid range — either crashing below `lower_bound` or surging
above `upper_bound` — the grid never fires again, but the open position
(accumulated stake from all buy fills) just sits there with no exit path.

Currently the only way to exit is to wait for the price to return to filled
levels.  In a strong trend this can take days or never happen.

## Goal

`GridTradingModule` detects a **range breakout** and triggers an orderly exit:
- Below-range: accumulated position is sold at market via `adjust_position`
  returning a large negative stake (full position liquidation).
- Above-range: all filled levels are cleared (profitable — let freqtrade's
  normal stoploss/ROI handle the rest).

## Acceptance Criteria

1. New config fields under `grid_trading_v1`:
   - `breakout_candles: int` (default 3) — price must stay outside range for
     this many consecutive candles before triggering exit.
   - `breakout_action: str` (`"exit"` | `"reset"` | `"none"`, default `"exit"`).

2. `GridState` gains `out_of_range_candles: int` (default 0), incremented each
   candle the price is outside `[lower_bound, upper_bound]`.  Reset to 0 when
   price returns in-range.

3. In `adjust_position`, after checking crossings:
   - If `not state.is_in_range(current_rate)`: increment `out_of_range_candles`.
   - If `out_of_range_candles >= breakout_candles`:
     - `"exit"`: return a large negative stake equal to the full open position
       size.  Log `"Grid breakout EXIT for {pair}: price {current_rate} outside
       [{lower}, {upper}] for {N} candles"`.  Clear grid state.
     - `"reset"`: call `_initialize_grid` with new centre = `current_rate`.
       Log `"Grid RESET for {pair}"`.
     - `"none"`: log warning, do nothing.

4. Tests:
   - 3 consecutive out-of-range candles with `breakout_action="exit"` returns
     a negative stake on the 3rd candle.
   - Price returning in-range resets `out_of_range_candles` to 0.
   - `breakout_action="none"` never returns an exit stake.
   - `breakout_action="reset"` re-centres the grid.

5. `to_dict` / `from_dict` include `out_of_range_candles` (persisted across
   bot restarts).

## Out of Scope

- Partial exits (exit only some filled levels)
- Breakout direction awareness (up vs down treated identically)

## Technical Notes

- The "exit" negative stake should be large enough that freqtrade closes the
  full position.  Use `-(trade.stake_amount * 10)` as a safe over-estimate;
  freqtrade will cap it at the actual position size.
- `breakout_action="reset"` is useful for ranging markets that temporarily
  spike and return — grid follows the price rather than dying.

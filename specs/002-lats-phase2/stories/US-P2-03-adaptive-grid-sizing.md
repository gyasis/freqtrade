# US-P2-03: Adaptive Grid Sizing via ATR

## Problem

`GridTradingModule` uses fixed percentage bounds (`upper_bound_pct`,
`lower_bound_pct`) to define the grid range.  In a low-volatility period a 5%
grid may be far too wide — most levels never get hit.  In a high-volatility
period the same 5% grid is too narrow and the price breaks out immediately,
making every grid fill a losing trade.

## Goal

Grid bounds are sized dynamically based on the pair's recent **Average True
Range (ATR)**, scaled to a configurable number of ATR multiples.  The grid
automatically widens in volatile markets and tightens in calm ones.

## Acceptance Criteria

1. New config field `grid_sizing_mode`: `"fixed"` (default, existing behaviour)
   or `"atr"`.

2. In `"atr"` mode:
   - `GridTradingModule.initialize` stores `atr_multiplier: float` (default 2.0)
     and `atr_period: int` (default 14).
   - When `_initialize_grid` is called, it reads `ctx.recent_candles` (from
     US-P2-01) to compute `ATR(atr_period)`.
   - `upper_bound = current_rate + atr * atr_multiplier`
   - `lower_bound = current_rate - atr * atr_multiplier`
   - Falls back to fixed-pct bounds when `recent_candles` is `None` or too short.

3. Grid levels are still evenly spaced within [lower, upper].

4. The initialised grid logs both the ATR value and the resulting bounds:
   ```
   Grid initialised for BTC/USDT: ATR=1234.56 lower=48765.44 upper=51234.56 count=5
   ```

5. Existing `"fixed"` mode is unchanged — all current tests pass.

6. New tests:
   - ATR-sized grid has bounds proportional to recent volatility.
   - Low-volatility df → narrower bounds than high-volatility df (same pair).
   - Graceful fallback when ATR cannot be computed.

## Out of Scope

- Continuous grid re-sizing after initialization (grid is fixed once opened)
- Per-pair ATR multiplier overrides

## Technical Notes

- Use `talib.ATR` if available; fall back to manual `(high - low).rolling(N).mean()`
  using pandas (no optional-dependency lock-in).
- ATR sizing requires US-P2-01 (candle history in context).

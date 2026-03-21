# US-P2-04: Bayesian Module Scoring with Live Trade Feedback

## Problem

`RuleBasedReasoningEngine` scores modules using only market indicators (ADX, BB
width, RSI) evaluated at the current candle.  It has no memory of how well a
module actually performed on this pair in the past.  A module can score 0.9 and
be selected even though it has produced 10 consecutive losing grid trades on
this pair.

As live trades come in, the system must update its belief about which module is
best suited for each pair, combining the static indicator signal with the
dynamic performance record.

## Goal

The reasoning engine maintains a **Bayesian prior** per `(module_id, pair)` that
is updated each time a trade closes.  The posterior score blends the indicator
signal with the empirical win-rate.

## Acceptance Criteria

1. `IReasoningEngine` gains a new optional method:
   ```python
   def record_trade_outcome(
       self, module_id: str, pair: str, profit_pct: float
   ) -> None: ...
   ```

2. `RuleBasedReasoningEngine` implements `record_trade_outcome`:
   - Maintains a per-`(module_id, pair)` running record: `wins`, `losses`,
     `total_profit_pct`.
   - A win is `profit_pct > 0`; a loss is `profit_pct <= 0`.

3. The final score blends indicator signal and Bayesian win-rate:
   ```
   indicator_score = _score_grid(df)          # 0.0 – 1.0 (existing)
   win_rate = wins / max(1, wins + losses)    # 0.0 – 1.0
   confidence = wins + losses                 # more trades = more confident
   weight = min(1.0, confidence / 20)         # ramp from 0 (no data) to 1 (20+ trades)
   final_score = (1 - weight) * indicator_score + weight * win_rate
   ```
   With fewer than 5 trades the indicator score dominates (weight < 0.25).

4. `OrchestratorStrategy.custom_exit` (or the `order_filled` hook for closed
   trades) calls `reasoning_engine.record_trade_outcome(module_id, pair, profit_pct)`.

5. The Bayesian state is persisted to `SharedState` under key
   `(reasoning_engine, "bayesian_priors")` and restored on bot start.

6. `record_trade_outcome` is a no-op (logged at DEBUG) when the engine is
   the base `IReasoningEngine` stub.

7. Tests:
   - After 10 wins, `win_rate` component pushes score above indicator-only score.
   - After 10 losses, score is pulled below indicator-only score.
   - Fresh state (0 trades) → score equals indicator score.
   - Persistence round-trip: saved and restored priors produce same scores.

## Out of Scope

- Full Bayesian inference with conjugate priors (beta distribution) — this story
  uses the simpler running-average approximation.
- Cross-pair generalisation (priors are strictly per-pair).

## Technical Notes

- This is the "live learning" story: it makes the system improve as real trades
  accumulate, turning each closed position into a data point.
- Depends on US-P2-01 indirectly (better context = better indicator scores to blend).
- `weight` ramp cap of 20 trades is configurable via `bayesian_min_trades` in
  the `reasoning` config block.

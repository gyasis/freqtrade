# Interface Contract: IReasoningEngine

**Version**: 1.0.0
**Branch**: `001-lats-algo-trading`

The reasoning engine determines which module has signal authority per pair per candle.

---

## Methods

### `initialize(config: dict, data_provider_proxy: DataProviderProxy) -> None`
Called once at bot startup. Load any pre-trained models or config.

### `score_modules(pair: str, df: DataFrame, active_module_ids: List[str]) -> Dict[str, float]`
Returns a confidence score (0.0–1.0) per active module for this pair at this candle.
- Higher score = more suitable for current market conditions
- Score below `module_score_threshold` (config) = module should NOT be activated
- All registered active modules must appear in the returned dict

### `get_routing_decision(pair: str, df: DataFrame) -> Optional[RoutingDecision]`
Returns the authoritative module for this pair.
- Returns `None` if no module scores above threshold → system holds
- Checks TTL of existing decision before re-evaluating

### `shutdown() -> None`
Flush any state, close model files cleanly.

---

## RuleBasedReasoningEngine (Phase 1 Implementation)

Scoring logic (no ML training required):

```
score = 0.0
if ADX(14) < 25:          score += 0.7   # ranging market → grid suitable
if bb_width < 0.04:       score += 0.3   # narrow bands → grid suitable
if 40 < RSI(14) < 60:     score += 0.1   # neutral momentum → grid suitable
score = min(1.0, score)

GridTradingModule score = score
# Future modules scored by their own regime logic
```

ADX column: `dataframe["adx"]` (talib.ADX, default period=14)
BB width column: `(bb_upper - bb_lower) / bb_mid`

---

## FreqAIReasoningEngine (Phase 3)

Uses FreqAI prediction pipeline:
- `set_freqai_targets()` produces `&-grid_suitable` label (1=ranging, 0=trending)
- `feature_engineering_expand_all()` adds regime features
- FreqAI trains and predicts → `df["&-grid_suitable"]` score fed into routing decision

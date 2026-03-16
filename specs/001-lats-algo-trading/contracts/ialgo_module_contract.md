# Interface Contract: IAlgoModule

**Version**: 1.0.0
**Branch**: `001-lats-algo-trading`

This is the primary interface every algorithm module must implement. The Orchestrator calls these methods exclusively — modules have no other entry points.

---

## Lifecycle Hooks

### `initialize(context: ModuleContext) -> None`
Called once when the module is registered. Set up internal state, validate config.
- **Must not** place orders or access market data
- **Must** raise `ValueError` if config is invalid (halts bot startup cleanly)

### `on_bot_start(context: ModuleContext) -> None`
Called after `initialize()`, after DataProvider is ready.
- Safe to load persisted state from `context.shared_state`
- Safe to call `context.data_provider.current_whitelist()`

### `shutdown(context: ModuleContext) -> None`
Called on bot shutdown or module deactivation.
- **Must** flush in-memory state to `context.shared_state`
- **Must not** raise — log and swallow all errors

---

## Per-Candle Signal Production

### `populate_indicators(df: DataFrame, metadata: dict, ctx: ModuleContext) -> DataFrame`
Adds module-specific indicator columns to the dataframe.
- **Must** prefix all new columns with `f"_{self.module_id}_"` to avoid collision
- **Must** return the dataframe (modified or unmodified)
- **Must not** modify existing columns from other modules

### `generate_entry_signal(df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal`
Returns the module's entry opinion for the current candle.
- Return `ModuleSignal()` (all None) if no opinion
- `entry_tag` **must** be prefixed `"{self.module_id}:"`
- `confidence` should reflect signal quality (0.0–1.0)

### `generate_exit_signal(df: DataFrame, metadata: dict, ctx: ModuleContext) -> ModuleSignal`
Returns the module's exit opinion for currently open positions on this pair.
- Return `ModuleSignal()` (all None) if no opinion
- `exit_tag` **must** be prefixed `"{self.module_id}:"`

---

## Trade Management (Optional Override)

### `adjust_position(trade, current_time, current_rate, current_profit, min_stake, max_stake, ctx) -> Optional[float]`
**Grid trading lives here.** Called by freqtrade for every open trade each candle.
- Return `None` → no action
- Return `+float` → buy more (must be between `min_stake` and `max_stake`)
- Return `-float` → partial sell
- Return `(float, str)` → value + order reason tag
- Only called if `supports_position_adjust == ModuleCapability.SUPPORTED`

### `custom_stoploss(pair, trade, current_time, current_rate, current_profit, ctx) -> Optional[float]`
- Return `None` → use strategy default stoploss
- Return `float` → stoploss as ratio from current rate (e.g. `-0.05` = 5% below)
- **Critical**: This method is NOT exception-isolated — errors propagate

### `on_order_filled(pair, trade, order, current_time, ctx) -> None`
Called when any order fills (entry, exit, adjustment).
- Use to update internal state (e.g. mark grid level as filled)
- **Must not** raise

---

## Introspection

### `get_module_state(pair: str) -> Dict`
Returns a JSON-serializable snapshot of per-pair state for persistence.

### `reset_module_state(pair: str) -> None`
Clears all state for a pair. Called when all positions for that pair are closed.

### Class Variables (must be set)

```python
module_id: ClassVar[str]          # unique slug, e.g. "grid_trading_v1"
version: ClassVar[str]            # semver, e.g. "1.0.0"
supports_backtest: ModuleCapability
supports_paper: ModuleCapability
supports_live: ModuleCapability
supports_position_adjust: ModuleCapability
supports_short: ModuleCapability
```

---

## Minimal Stub (for testing new modules)

```python
class MyNewModule(IAlgoModule):
    module_id = "my_module_v1"
    version = "0.1.0"
    supports_backtest = ModuleCapability.SUPPORTED
    supports_paper = ModuleCapability.SUPPORTED
    supports_live = ModuleCapability.SUPPORTED
    supports_position_adjust = ModuleCapability.UNSUPPORTED
    supports_short = ModuleCapability.UNSUPPORTED

    def initialize(self, ctx): pass
    def on_bot_start(self, ctx): pass
    def shutdown(self, ctx): pass
    def populate_indicators(self, df, meta, ctx): return df
    def generate_entry_signal(self, df, meta, ctx): return ModuleSignal()
    def generate_exit_signal(self, df, meta, ctx): return ModuleSignal()
    def get_module_state(self, pair): return {}
    def reset_module_state(self, pair): pass
```

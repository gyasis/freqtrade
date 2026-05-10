# Module Interface Contract: Grid Trading Core

**Feature**: 003-grid-calculator-unify
**Contract type**: Internal Python module API
**Portability**: GridConfig, GridCalculator, GridState 
 usable without freqtrade

---

## `GridConfig` (config/grid_config.py)

### Construction

```python
from algo_system.config.grid_config import GridConfig

# Defaults (linear, moving-average midprice)
cfg = GridConfig()

# Log-scale grid
cfg = GridConfig(log_scale=True, grid_distance=0.01, grid_range=0.30)

# Auto-adjust from ATR
cfg = GridConfig(auto_adjust=True, auto_set_grid=True)

# VWAP midprice
cfg = GridConfig(method="vwap", period=20)

# Validate before use (raises ValueError on degenerate config)
cfg.validate()
```

### Serialization

```python
d: dict = cfg.to_dict()          # JSON-serializable
cfg2 = GridConfig.from_dict(d)   # reconstruct
assert cfg == cfg2
```

### Constants (importable from same module)

```python
from algo_system.config.grid_config import (
    VALID_METHODS,
    VALID_ALLOCATION_STRATEGIES,
    VALID_INDICATORS,
)
```

---

## `GridCalculator` (modules/grid_trading/grid_calculator.py)

### Level generation

```python
from algo_system.modules.grid_trading.grid_calculator import GridCalculator
import pandas as pd

cfg = GridConfig(log_scale=False, grid_distance=0.01, grid_range=0.10)

# Without candle data (linear, market_price midprice only)
levels: list[float] = GridCalculator.generate_levels(cfg, midprice=50000.0)

# With candle data (enables VWAP, MA midprice, auto_adjust, log_scale+ATR)
df: pd.DataFrame = ...  # OHLCV candles
midprice = GridCalculator.calculate_midprice(df, cfg)
levels = GridCalculator.generate_levels(cfg, midprice, df=df)
```

### Crossing detection

```python
# Buy triggers: levels crossed downward
buy_levels: list[float] = GridCalculator.levels_crossed_down(
    from_price=50100.0,
    to_price=49900.0,
    levels=levels,
)

# Sell triggers: filled levels crossed upward
sell_levels: list[float] = GridCalculator.levels_crossed_up(
    from_price=49900.0,
    to_price=50100.0,
    levels=levels,
    filled={50000.0},
)

# Navigation
nearest = GridCalculator.nearest_level(50050.0, levels)
below   = GridCalculator.level_below(50050.0, levels)
above   = GridCalculator.level_above(50050.0, levels)
```

### Allocation

```python
stake = GridCalculator.calculate_allocation(
    config=cfg,
    trade_value=10000.0,
    current_price=50000.0,
    atr=500.0,       # required only for "volatility" strategy
    available=5000.0,
)
```

---

## `GridState` (modules/grid_trading/grid_state.py)

### Construction

```python
from algo_system.modules.grid_trading.grid_state import GridState
from datetime import datetime, timezone

state = GridState(
    pair="BTC/USDT",
    upper_bound=52000.0,
    lower_bound=48000.0,
    grid_levels=[48000.0, 49000.0, 50000.0, 51000.0, 52000.0],
    filled_levels=set(),
    initial_entry_price=50000.0,
    last_price=None,          # None until first candle cycle
    created_at=datetime.now(timezone.utc),
)
```

### Lifecycle

```python
# First candle after initialization
state.update_last_price(50000.0)
assert state.is_active()   # True after first price update

# Crossing events
state.mark_filled(49000.0)
state.mark_unfilled(49000.0)

# State snapshot
state_dict = state.to_dict()

# Restore
restored = GridState.from_dict(state_dict)
```

### Properties

```python
state.grid_count    # 
 int: len(grid_levels) 
 derived property, not stored
state.is_active()   # 
 bool: last_price is not None
state.is_in_range(price)  # 
 bool: lower_bound <= price <= upper_bound
state.is_level_filled(level)  # 
 bool
state.get_unfilled_levels()   # 
 List[float] sorted asc
state.get_filled_levels_sorted()  # 
 List[float] sorted asc
```

---

## `GridTradingModule` (freqtrade adapter)

This class requires freqtrade to be installed. It implements `IAlgoModule` and is not portable.

```python
from algo_system.modules.grid_trading.grid_trading_module import GridTradingModule

module = GridTradingModule()
# Used exclusively via the IAlgoModule interface:
# module.initialize(ctx)
# module.on_bot_start(ctx)
# module.populate_indicators(df, metadata, ctx)
# module.generate_entry_signal(df, metadata, ctx)
# module.generate_exit_signal(df, metadata, ctx)
# module.adjust_position(trade, time, rate, profit, min_stake, max_stake, ctx)
# module.on_order_filled(pair, trade, order, time, ctx)
# module.get_module_state(pair)
# module.reset_module_state(pair)
# module.shutdown(ctx)
```

---

## Breaking Changes from Current API

| Old | New | Notes |
|-----|-----|-------|
| `GridConfigV2` | `GridConfig` | Rename only 
 fields identical |
| `from grid_calculator_v2 import GridConfigV2` | `from config.grid_config import GridConfig` | Import path change |
| `GridCalculator.validate_grid(lower, upper, count)` | `GridConfig.validate()` | Moved to config |
| `GridCalculator.calculate_levels(lower, upper, count)` | `GridCalculator.generate_levels(config, midprice, df=None)` | Signature change |
| `GridCalculator.get_crossed_levels_down(...)` | `GridCalculator.levels_crossed_down(...)` | Renamed (drop `get_`) |
| `GridCalculator.get_crossed_levels_up(...)` | `GridCalculator.levels_crossed_up(...)` | Renamed (drop `get_`) |
| `GridCalculator.get_nearest_level(...)` | `GridCalculator.nearest_level(...)` | Renamed (drop `get_`) |
| `GridCalculator.get_level_below(...)` | `GridCalculator.level_below(...)` | Renamed (drop `get_`) |
| `GridCalculator.get_level_above(...)` | `GridCalculator.level_above(...)` | Renamed (drop `get_`) |
| `generate_grid_levels(...)` (module-level fn) | `GridCalculator.generate_levels(...)` | Now a static method |
| `calculate_midprice(...)` (module-level fn) | `GridCalculator.calculate_midprice(...)` | Now a static method |
| `GridState.grid_count` (field) | `GridState.grid_count` (property) | Same name, different storage |
| `GridState.is_active()` | `GridState.is_active()` | Different semantics: `last_price is not None` |
| `GridTradingModule._last_prices` | `GridState.last_price` | State moved into GridState |
| `GridTradingModule._v2_configs` | `GridTradingModule._configs` | Renamed (drop version suffix) |

# Research: Layered Algorithmic Trading System (LATS)
**Branch**: `001-lats-algo-trading`
**Date**: 2026-03-16
**Status**: Partial — awaiting Phase 0 Agent 2 results (plugin/SM patterns)

---

## R-001: adjust_trade_position — Exact Contract

**Decision**: Grid trading uses `adjust_trade_position()` exclusively for all grid-level orders.

**Confirmed Signature** (`interface.py:617`):
```python
def adjust_trade_position(
    self, trade: Trade, current_time: datetime,
    current_rate: float, current_profit: float,
    min_stake: Optional[float], max_stake: float,
    current_entry_rate: float, current_exit_rate: float,
    current_entry_profit: float, current_exit_profit: float,
    **kwargs,
) -> Union[Optional[float], Tuple[Optional[float], Optional[str]]]
```

**Return semantics**:
- `None` → no action
- `+float` → buy more (grid level down-cross)
- `-float` → partial sell (grid level up-cross on a filled level)
- `(float, str)` → value + order reason tag (use this: `(-stake, "grid:level_3_exit")`)

**Requirement**: `position_adjustment_enable: bool = True` must be set as a **class variable** on `OrchestratorStrategy` (not a config key). `interface.py:113`.

**Rationale**: This is the only freqtrade-native hook for incremental position building/reduction. Implementing grid via separate entry signals per level would require multiple open trades per pair and is not the correct pattern.

---

## R-002: enter_tag Attribution

**Decision**: All module entry tags use the format `"{module_id}:{signal_reason}"`.

**Confirmed constraints** (`constants.py:96`, `trade_model.py:429`):
- Max length: **255 characters** (`CUSTOM_TAG_MAX_LENGTH = 255`)
- Type: `Optional[String(255)]`
- Auto-truncated by `@validates` decorator — no crash on overflow, silent truncation
- Stored on both `LocalTrade` (backtest) and `Trade` (live/dry-run) models

**Format adopted**: `"grid:initial_entry"`, `"grid:level_3_buy"`, `"grid:level_5_sell"` — all under 30 chars, well within limit.

**Rationale**: enter_tag is the only per-trade attribution mechanism in freqtrade. With 255-char limit and auto-truncation, the `"{module_id}:{reason}"` format is safe and unambiguous for P&L attribution queries.

---

## R-003: Exception Isolation — strategy_safe_wrapper Pattern

**Decision**: All module calls in `OrchestratorStrategy` are wrapped using the same pattern as freqtrade's own `strategy_safe_wrapper`.

**Confirmed implementation** (`strategy_wrapper.py:15`):
```python
strategy_safe_wrapper(f, message="", default_retval=None, supress_error=False)
```
- Catches `ValueError` + general `Exception`
- Deep-copies `trade` kwarg before passing to prevent accidental mutation
- Returns `default_retval` on exception (if not None) or raises `StrategyError`
- Usage: `strategy_safe_wrapper(self.strategy.bot_loop_start, supress_error=True)(current_time)`

**Our equivalent for module calls**:
```python
def _safe_module_call(self, module, method_name, *args, default=None, **kwargs):
    try:
        return getattr(module, method_name)(*args, **kwargs)
    except Exception as e:
        self._record_module_failure(module.module_id)
        logger.exception(f"Module {module.module_id}.{method_name} raised: {e}")
        return default
```

**Rationale**: Mirrors freqtrade's own pattern exactly. `supress_error=True` + `default_retval` is the correct idiom for non-critical hooks; critical hooks (stoploss) should NOT suppress errors.

---

## R-004: RunMode — Capability Mapping

**Confirmed values** (`runmode.py`):

| RunMode | Value | LATS Module Capability |
|---------|-------|----------------------|
| `LIVE` | `"live"` | `ModuleCapability.LIVE` |
| `DRY_RUN` | `"dry_run"` | `ModuleCapability.PAPER` |
| `BACKTEST` | `"backtest"` | `ModuleCapability.BACKTEST` |
| `HYPEROPT` | `"hyperopt"` | `ModuleCapability.HYPEROPT` |
| `EDGE` | `"edge"` | Not supported (out of scope) |

**Trade mode group**: `RunMode.TRADE_MODES = [LIVE, DRY_RUN]` — use this for live/paper guards.

**Accessed via**: `self.dp.runmode` (DataProvider property, `dataprovider.py:403`)

---

## R-005: DataProvider — Available Methods for Modules

**Decision**: `ModuleContext` exposes a `DataProviderProxy` facade with only these methods:

| Method | Signature | Use in LATS |
|--------|-----------|-------------|
| `ohlcv()` | `(pair, timeframe, copy=True)` | Live candle data for modules |
| `get_pair_dataframe()` | `(pair, timeframe)` | Backtest + live unified candle access |
| `runmode` | `@property → RunMode` | Module capability gating |
| `current_whitelist()` | `() → List[str]` | Reasoning layer pair iteration |
| `historic_ohlcv()` | `(pair, timeframe)` | Backtesting historical data |

**Rationale**: Exposing the full DataProvider to modules creates tight coupling. The proxy facade limits the surface area and makes mocking trivial in tests.

---

## R-006: Wallets — Balance and Position Access

**Decision**: `ModuleContext` exposes a `WalletsProxy` with these methods:

| Method | Use in LATS |
|--------|-------------|
| `get_available_stake_amount()` | BudgetAllocator checks before entry |
| `get_total_stake_amount()` | BudgetAllocator calculates allocation percentages |
| `get_trade_stake_amount(pair, max_open_trades)` | Per-trade stake sizing |
| `get_free(currency)` | Direct balance check |

**Rationale**: `get_available_stake_amount()` already respects `tradable_balance_ratio` — the BudgetAllocator should apply its per-module cap ON TOP of this value.

---

## R-007: Trade Model — Key Fields for Attribution

**Decision**: Trade attribution uses `enter_tag`. No new database fields are added to freqtrade's Trade model.

**Key fields available** (`trade_model.py`):

| Field | Type | LATS Use |
|-------|------|----------|
| `pair` | str(25) | Module state key lookup |
| `enter_tag` | str(255) | Module ID attribution |
| `open_rate` | float | Grid calculator reference price |
| `stake_amount` | float | Budget allocator tracking |
| `is_open` | bool | Draining state completion check |
| `is_short` | bool | Module capability SHORT check |

**Open trade count**: `Trade.get_open_trade_count()` (static method, `trade_model.py:1473`)
**Open trade list**: `Trade.get_trades_proxy(is_open=True)`

**Rationale**: Adding new columns to Trade would require database migrations and freqtrade version coupling. Using `enter_tag` prefix convention is fully backward compatible.

---

## R-008: FreqaiDataDrawer pair_dict — SharedState Pattern

**Confirmed structure** (`data_drawer.py:37`):
```python
class pair_info(TypedDict):
    model_filename: str
    trained_timestamp: int
    data_path: str
    extras: dict  # ← extensibility slot
```

**SharedState mirrors this pattern**:
```python
class ModuleStateEntry(TypedDict):
    module_id: str
    pair: str
    updated_timestamp: int
    data: dict  # module-specific state blob
```

**Persistence**: `FreqaiDataDrawer` uses `pair_dictionary.json` + `pair_dict_lock` (threading.Lock). SharedState mirrors this: JSON file + threading.Lock for thread safety in live mode.

---

## R-009: bot_loop_start — Orchestrator Use

**Confirmed signature** (`interface.py:278`):
```python
def bot_loop_start(self, current_time: datetime, **kwargs) -> None
```

**LATS use**: Called once per trading loop iteration. The `OrchestratorStrategy` uses this to:
1. Let the ReasoningEngine observe market-wide state (circuit breaker evaluation)
2. Update module activity scores (TTL expiry checks)
3. Flush SharedState metrics to MetricsCollector

**Note**: This is pair-independent — do NOT access pair-specific data here. Use `populate_indicators()` for per-pair work.

---

---

## R-010: State Machine — Enum Pattern (No External Deps)

**Decision**: Use `class ModuleState(str, Enum)` with grouped state constants.

**Pattern source**: `freqtrade/enums/state.py` + `freqtrade/enums/runmode.py`

```python
class ModuleState(str, Enum):
    INACTIVE   = "inactive"
    ACTIVE     = "active"
    DRAINING   = "draining"    # closing positions before switch
    SWITCHING  = "switching"   # new module taking over
    SUSPENDED  = "suspended"   # error threshold exceeded

    def __str__(self):
        return self.value

# Helper groups (mirrors RunMode.TRADE_MODES pattern)
MANAGED_STATES = (ModuleState.ACTIVE, ModuleState.DRAINING, ModuleState.SWITCHING)
```

**Transitions enforced by explicit guard method** — no transitions library needed:
```python
VALID_TRANSITIONS = {
    ModuleState.INACTIVE:  {ModuleState.ACTIVE},
    ModuleState.ACTIVE:    {ModuleState.DRAINING, ModuleState.SUSPENDED},
    ModuleState.DRAINING:  {ModuleState.SWITCHING, ModuleState.SUSPENDED},
    ModuleState.SWITCHING: {ModuleState.ACTIVE, ModuleState.SUSPENDED},
    ModuleState.SUSPENDED: {ModuleState.INACTIVE},
}
```

**Rationale**: Freqtrade uses this pattern throughout. Zero dependencies. 5 states fit perfectly in an Enum. The `transitions` library adds ~10KB for no gain here.

---

## R-011: Plugin Discovery — IResolver Pattern to Mirror

**Decision**: `ModuleResolver` inherits `IResolver` and mirrors `PairListResolver`.

**Source**: `resolvers/iresolver.py:75-122`, `resolvers/pairlist_resolver.py`

**Core algorithm** (`iresolver.py:88`):
```python
spec = importlib.util.spec_from_file_location(module_name, str(module_path))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

valid_objects = [
    obj for name, obj in inspect.getmembers(module, inspect.isclass)
    if issubclass(obj, IAlgoModule)        # ABC match
    and obj is not IAlgoModule             # Exclude base
    and obj.__module__ == module_name      # Defined in THIS file only
]
```

**PathModifier context manager** (`iresolver.py:21`): Adds module directory to `sys.path` on enter, removes on exit — prevents sys.path pollution.

**Directory scan** (`iresolver.py:235`):
```python
for entry in directory.iterdir():
    if entry.suffix != ".py": continue
    if entry.is_symlink() and not entry.is_file(): continue
```

**LATS ModuleResolver**:
```python
class ModuleResolver(IResolver):
    object_type = IAlgoModule
    object_type_str = "AlgoModule"
    user_subdir = "modules"
    initial_search_path = Path(__file__).parent.parent / "modules"
    extra_path = "algo_system.modules_path"
```

**Rationale**: Directly reusing freqtrade's own resolver pattern ensures we get the same error handling, path searching, and class detection that the rest of freqtrade relies on.

---

## R-012: Technical Indicators — Use talib, NOT pandas-ta

**Decision**: `RuleBasedReasoningEngine` uses `talib.ADX()` and `qtpylib.bollinger_bands()` — matching freqtrade's own templates.

**Source**: `templates/sample_strategy.py:160-244`

**ADX**:
```python
import talib.abstract as ta
dataframe["adx"] = ta.ADX(dataframe)   # column name: "adx", default period=14
```

**Bollinger Bands** (via `technical` library, already a freqtrade dep):
```python
from technical import qtpylib
bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
dataframe["bb_lower"] = bollinger["lower"]
dataframe["bb_upper"] = bollinger["upper"]
dataframe["bb_mid"]   = bollinger["mid"]
# Width (for regime detection):
dataframe["bb_width"] = (bollinger["upper"] - bollinger["lower"]) / bollinger["mid"]
```

**Regime scoring** (RuleBasedReasoningEngine):
- ADX < 25 → ranging → grid score += 0.7
- BB width < threshold (e.g. 0.04) → narrow range → grid score += 0.3
- Both conditions → grid score = 1.0 (high confidence)

**CRITICAL**: Do NOT use `pandas-ta` for these. freqtrade uses `talib` + `technical`. Column names differ and pandas-ta adds an unnecessary dependency divergence.

---

## R-013: Module Capability Declaration — IPairList Pattern

**Decision**: `IAlgoModule` uses class-level `ModuleCapability` enum vars, checked at runtime by the orchestrator.

**Source**: `plugins/pairlist/IPairList.py:55-68`, `plugins/pairlistmanager.py:66-97`

```python
class ModuleCapability(str, Enum):
    SUPPORTED     = "supported"
    UNSUPPORTED   = "unsupported"
    PARTIAL       = "partial"      # works but with limitations

class IAlgoModule(ABC):
    # Class-level capability declarations (override in subclasses)
    supports_backtest:   ModuleCapability = ModuleCapability.SUPPORTED
    supports_paper:      ModuleCapability = ModuleCapability.SUPPORTED
    supports_live:       ModuleCapability = ModuleCapability.SUPPORTED
    supports_hyperopt:   ModuleCapability = ModuleCapability.PARTIAL
    supports_short:      ModuleCapability = ModuleCapability.UNSUPPORTED
    supports_position_adjust: ModuleCapability = ModuleCapability.SUPPORTED
```

**Orchestrator checks at activation** (mirrors `pairlistmanager.py:_check_backtest`):
```python
def _validate_module_for_runmode(self, module: IAlgoModule, runmode: RunMode) -> bool:
    cap_map = {
        RunMode.BACKTEST: module.supports_backtest,
        RunMode.DRY_RUN:  module.supports_paper,
        RunMode.LIVE:     module.supports_live,
    }
    return cap_map.get(runmode, ModuleCapability.UNSUPPORTED) != ModuleCapability.UNSUPPORTED
```

---

## R-014: RPC Alerts — dp.send_msg() Pattern

**Decision**: Module suspension and circuit breaker alerts use `self.dp.send_msg()` — the standard strategy messaging channel.

**Source**: `data/dataprovider.py:570`, `freqtradebot.py:296`, `rpc/rpc_manager.py:87`

**Flow**:
1. `OrchestratorStrategy` calls: `self.dp.send_msg(f"[LATS] Module {module_id} SUSPENDED after 3 failures")`
2. DataProvider queues the message (deduped per candle unless `always_send=True`)
3. Main bot loop: `self.rpc.process_msg_queue(self.dataprovider._msg_queue)`
4. RPCManager distributes to all registered handlers (Telegram, Discord, webhook)

**Only works in `DRY_RUN` and `LIVE` mode** — silently no-ops in `BACKTEST`. This is correct behavior.

**For critical alerts (always deliver)**:
```python
self.dp.send_msg("[LATS] CIRCUIT BREAKER TRIGGERED — all trading halted", always_send=True)
```

**Message types used**:
- `RPCMessageType.STRATEGY_MSG` — custom strategy messages (our channel)
- Handled automatically by `dp.send_msg()` — no direct RPC access needed

---

---

## R-015: RL Environments for LATS Meta-Reasoning Layer

**Decision**: Build a custom `MyRLEnv` subclassing freqtrade's `BaseEnvironment` directly — do NOT use `Base3/4/5ActionRLEnv`.

**Critical distinction**: Standard FreqAI RL = agent picks *trade actions* (enter/exit). LATS RL = agent picks *which algo module to activate*. These require different action spaces.

**Recommended environment design**:
```python
class LATSModuleSelectionEnv(BaseEnvironment):
    """Meta-RL env: action = which algo module to activate."""

    def set_action_space(self):
        # N = number of registered algo modules
        self.action_space = spaces.Discrete(len(self.module_ids))

    def calculate_reward(self, action: int) -> float:
        # Reward = risk-adjusted return of selected module over window
        selected_module = self.module_ids[action]
        return self._module_sharpe(selected_module)

    def is_tradesignal(self, action: int) -> bool:
        return True  # all actions are valid selections
```

**Observation space**: Market regime features — ATR, ADX, BB width, volume profile, recent correlation.

**Environment comparison**:

| Environment | Maintained | Gym API | LATS Fit | Notes |
|-------------|-----------|---------|----------|-------|
| freqtrade `BaseEnvironment` | Yes (active develop) | Gymnasium (Farama) | **Best** | Already in stack, live bridge, MaskablePPO |
| FinRL | Yes (AI4Finance) | Gymnasium | Medium | Better for portfolio/multi-asset research |
| gymnasium-anytrading | Moderate | Gymnasium | Low | Single asset, limited customization |
| TradingGym | Low (unmaintained) | Legacy OpenAI gym | None | Avoid |
| FinGym | Academic only | Legacy OpenAI gym | None | Avoid |

**Gymnasium (Farama Foundation) status**: Actively maintained as of Aug 2025. freqtrade already imports `gymnasium` (not legacy `gym`). Farama released v1.0 stable in 2024. SB3 and sb3-contrib both target Gymnasium as primary API.

**Supported SB3 algorithms** (already in `requirements-freqai-rl.txt`):
- `PPO`, `A2C`, `DQN` (stable-baselines3)
- `TRPO`, `RecurrentPPO`, `MaskablePPO`, `QRDQN` (sb3-contrib)
- **`MaskablePPO` recommended for LATS** — can mask invalid module selections (e.g. grid module masked when trending, and the module declares `UNSUPPORTED` for that regime)

**Key freqtrade RL files**:
- `freqtrade/freqai/RL/BaseEnvironment.py` — base class to subclass
- `freqtrade/freqai/RL/BaseReinforcementLearningModel.py` — SB3 wrapper with SubprocVecEnv, continual learning, TensorBoard
- `freqtrade/freqai/prediction_models/ReinforcementLearner.py` — override `MyRLEnv` here

**Phase 3 plan**: Subclass `BaseEnvironment` → action space = `Discrete(N modules)` → reward = selected module's risk-adjusted return → train with `MaskablePPO` → replace `RuleBasedReasoningEngine` via config.

---

## Research Complete ✓

All NEEDS CLARIFICATION items resolved. No blockers. Ready for Phase 1 design.

**Summary of key decisions**:
| Decision | Choice | Source |
|----------|--------|--------|
| Grid orders | `adjust_trade_position()` only | `interface.py:617` |
| Trade attribution | `enter_tag` prefix `"{module_id}:"` | `trade_model.py:429` |
| Exception isolation | Mirror `strategy_safe_wrapper` | `strategy_wrapper.py:15` |
| State machine | Enum + VALID_TRANSITIONS dict | `enums/state.py` pattern |
| Plugin discovery | Mirror `IResolver._get_valid_object()` | `resolvers/iresolver.py:75` |
| Indicators | `talib.ADX()` + `qtpylib.bollinger_bands()` | `templates/sample_strategy.py` |
| Capability flags | Class-level enum vars | `plugins/pairlist/IPairList.py:55` |
| RPC alerts | `dp.send_msg()` | `data/dataprovider.py:570` |

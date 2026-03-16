# Data Model: Layered Algorithmic Trading System (LATS)
**Branch**: `001-lats-algo-trading`
**Date**: 2026-03-16

---

## Entity Relationship Overview

```
OrchestratorStrategy
  ├── ModuleRegistry ──────────────────► IAlgoModule (ABC)
  │     └── instances: Dict[str, IAlgoModule]    └── GridTradingModule
  ├── ReasoningEngine ──────────────────────────────── RuleBasedReasoningEngine
  │     └── produces: RoutingDecision
  ├── BudgetAllocator
  │     └── budgets: Dict[str, ModuleBudget]
  ├── SignalArbiter
  │     └── priority: List[str]
  ├── AlgoLifecycleSM
  │     └── states: Dict[str, Dict[str, ModuleState]]  # {module_id: {pair: state}}
  ├── CircuitBreaker
  │     └── breaker_state: CircuitBreakerState
  └── SharedState
        └── store: Dict[Tuple[str,str], Any]  # {(module_id, pair): data}

GridTradingModule
  └── grid_states: Dict[str, GridState]  # {pair: GridState}
        └── calculator: GridCalculator (stateless)

ModuleContext (created per-call, not persisted)
  ├── DataProviderProxy
  ├── WalletsProxy
  ├── SharedState (reference)
  └── reasoning_hints: Optional[RoutingDecision]
```

---

## Entity Definitions

### 1. ModuleState

Governs the lifecycle of a module per trading pair.

```python
class ModuleState(str, Enum):
    INACTIVE   = "inactive"    # Not yet started or cleanly shut down
    ACTIVE     = "active"      # Running, producing signals
    DRAINING   = "draining"    # Closing positions, stoploss still managed
    SWITCHING  = "switching"   # New module taking over, transition in progress
    SUSPENDED  = "suspended"   # Auto-suspended after 3 consecutive failures

# Valid state transitions
VALID_TRANSITIONS: Dict[ModuleState, Set[ModuleState]] = {
    ModuleState.INACTIVE:   {ModuleState.ACTIVE},
    ModuleState.ACTIVE:     {ModuleState.DRAINING, ModuleState.SUSPENDED},
    ModuleState.DRAINING:   {ModuleState.SWITCHING, ModuleState.SUSPENDED},
    ModuleState.SWITCHING:  {ModuleState.ACTIVE, ModuleState.SUSPENDED},
    ModuleState.SUSPENDED:  {ModuleState.INACTIVE},
}

# Groups
MANAGED_STATES = (ModuleState.ACTIVE, ModuleState.DRAINING, ModuleState.SWITCHING)
```

---

### 2. ModuleCapability

Class-level declaration on each `IAlgoModule` subclass.

```python
class ModuleCapability(str, Enum):
    SUPPORTED   = "supported"
    UNSUPPORTED = "unsupported"
    PARTIAL     = "partial"     # works with limitations

# Used as class variables on IAlgoModule:
class IAlgoModule(ABC):
    supports_backtest:        ModuleCapability = ModuleCapability.SUPPORTED
    supports_paper:           ModuleCapability = ModuleCapability.SUPPORTED
    supports_live:            ModuleCapability = ModuleCapability.SUPPORTED
    supports_hyperopt:        ModuleCapability = ModuleCapability.PARTIAL
    supports_short:           ModuleCapability = ModuleCapability.UNSUPPORTED
    supports_position_adjust: ModuleCapability = ModuleCapability.SUPPORTED
```

---

### 3. ModuleSignal

Typed output of a module's per-candle signal evaluation. Created fresh each candle.

```python
@dataclass
class ModuleSignal:
    enter_long:  Optional[bool] = None   # None = no opinion
    enter_short: Optional[bool] = None
    exit_long:   Optional[bool] = None
    exit_short:  Optional[bool] = None
    entry_tag:   Optional[str]  = None   # MUST be prefixed "{module_id}:"
    exit_tag:    Optional[str]  = None   # MUST be prefixed "{module_id}:"
    confidence:  float = 1.0             # 0.0–1.0, used by SignalArbiter
    metadata:    Dict[str, Any] = field(default_factory=dict)

    def has_entry_signal(self) -> bool:
        return self.enter_long is True or self.enter_short is True

    def has_exit_signal(self) -> bool:
        return self.exit_long is True or self.exit_short is True
```

**Constraints**:
- `entry_tag` max 255 chars (freqtrade `CUSTOM_TAG_MAX_LENGTH`)
- `entry_tag` format: `"{module_id}:{reason}"` e.g. `"grid:initial_entry"`

---

### 4. RoutingDecision

Output of the ReasoningEngine. Identifies which module has signal authority.

```python
@dataclass
class RoutingDecision:
    authoritative_module_id: str
    confidence: float                   # 0.0–1.0
    rationale: str                      # human-readable, logged + sent via RPC on change
    valid_for_candles: int              # TTL: re-evaluate after this many candles
    candles_remaining: int              # countdown, decremented each candle
    fallback_module_id: Optional[str] = None

    def is_expired(self) -> bool:
        return self.candles_remaining <= 0

    def tick(self) -> None:
        self.candles_remaining = max(0, self.candles_remaining - 1)
```

---

### 5. GridState

Per-pair mutable state for `GridTradingModule`. Persisted to disk via `SharedState`.

```python
@dataclass
class GridState:
    pair: str
    upper_bound: float
    lower_bound: float
    grid_count: int
    grid_levels: List[float]            # sorted ascending, calculated at init
    filled_levels: Set[float]           # levels with active/filled buy orders
    initial_entry_price: Optional[float] = None
    created_at: Optional[datetime] = None
    last_updated: Optional[datetime] = None

    def is_level_filled(self, level: float) -> bool:
        return level in self.filled_levels

    def mark_filled(self, level: float) -> None:
        self.filled_levels.add(level)
        self.last_updated = datetime.now(timezone.utc)

    def mark_unfilled(self, level: float) -> None:
        self.filled_levels.discard(level)
        self.last_updated = datetime.now(timezone.utc)

    def is_in_range(self, price: float) -> bool:
        return self.lower_bound <= price <= self.upper_bound
```

**Validation rules**:
- `upper_bound > lower_bound`
- `grid_count >= 2`
- `len(grid_levels) == grid_count`
- `filled_levels ⊆ set(grid_levels)`

---

### 6. ModuleBudget

Per-module capital allocation enforced by `BudgetAllocator`.

```python
@dataclass
class ModuleBudget:
    module_id: str
    max_open_trades: int                # hard cap on open trade count
    stake_allocation_pct: float         # fraction of total capital (0.0–1.0)
    current_open_trades: int = 0        # tracked in real-time
    current_stake_used: float = 0.0     # tracked in real-time

    def can_open_trade(self, proposed_stake: float, total_capital: float) -> bool:
        if self.current_open_trades >= self.max_open_trades:
            return False
        allocated_max = total_capital * self.stake_allocation_pct
        if self.current_stake_used + proposed_stake > allocated_max:
            return False
        return True
```

---

### 7. CircuitBreakerState

```python
class CircuitBreakerStatus(str, Enum):
    ARMED    = "armed"     # monitoring, ready to trigger
    TRIPPED  = "tripped"   # halt state, no new orders
    COOLING  = "cooling"   # recovering, monitoring for reset

@dataclass
class CircuitBreakerState:
    status: CircuitBreakerStatus = CircuitBreakerStatus.ARMED
    tripped_at: Optional[datetime] = None
    trip_reason: Optional[str] = None
    max_drawdown_pct: float = 0.15
    price_move_pct: float = 0.08
    price_move_candles: int = 3

    def is_active(self) -> bool:
        return self.status == CircuitBreakerStatus.TRIPPED
```

---

### 8. SharedState Entry

Mirrors `FreqaiDataDrawer.pair_info` TypedDict pattern.

```python
class ModuleStateEntry(TypedDict):
    module_id: str
    pair: str
    updated_timestamp: int       # Unix timestamp
    data: dict                   # module-specific state blob (e.g. GridState as dict)

# Store key: (module_id, pair)
# e.g. ("grid_trading_v1", "BTC/USDT") → ModuleStateEntry
```

**Persistence**: JSON file at `user_data/algo_system_state.json`, thread-safe via `threading.Lock`.

---

### 9. ModuleContext (ephemeral, not persisted)

Created fresh for each module call. Provides a clean facade over freqtrade internals.

```python
@dataclass
class ModuleContext:
    pair: str
    run_mode: RunMode
    current_time: datetime
    data_provider: DataProviderProxy     # facade over freqtrade DataProvider
    wallets: WalletsProxy                # facade over freqtrade Wallets
    shared_state: SharedState
    reasoning_hints: Optional[RoutingDecision]
    logger: logging.Logger               # namespaced: algo_system.{module_id}.{pair}
    module_id: str
```

---

### 10. AlgoSystemConfig (runtime config)

Parsed from `config["algo_system"]` at bot startup. Validated at load time.

```python
class AlgoSystemConfig(TypedDict):
    modules_path: str
    active_modules: List[str]
    default_module: str
    module_switch_cooldown_candles: int
    reasoning: ReasoningConfig
    modules: Dict[str, Dict[str, Any]]   # per-module config blobs
    circuit_breaker: CircuitBreakerConfig
    shared_state: SharedStateConfig
    observability: ObservabilityConfig
```

---

## State Transition Diagram

```
                    ┌──────────┐
                    │ INACTIVE │◄──────────────────────┐
                    └────┬─────┘                       │
                         │ activate()                  │
                         ▼                             │
                    ┌──────────┐  3x failures          │
                    │  ACTIVE  ├──────────────────► SUSPENDED
                    └────┬─────┘                       │
                         │ switch_requested()           │ reset()
                         ▼                             │
                    ┌──────────┐  3x failures          │
                    │ DRAINING ├──────────────────► SUSPENDED
                    └────┬─────┘
                         │ all_positions_closed()
                         ▼
                    ┌──────────┐  3x failures
                    │SWITCHING ├──────────────────► SUSPENDED
                    └────┬─────┘
                         │ new_module_ready()
                         ▼
                    ┌──────────┐
                    │  ACTIVE  │ (new module)
                    └──────────┘
```

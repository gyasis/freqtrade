# US-P2-02: Streamlined Configuration (Config Profiles)

## Problem

The current `algo_system` config block requires 20+ fields spread across 5
sub-blocks (`reasoning`, `circuit_breaker`, `entry_quality`, `pair_selection`,
`modules`).  Operators starting out must understand all five areas before they
can run even a basic backtest.  Small mistakes — wrong key name, missing
required field — surface only at bot startup as `ValueError`.

## Goal

Introduce **config profiles** (`conservative`, `moderate`, `aggressive`) that
provide a single sensible baseline.  Operators override only what they
actually want to change.  A valid minimal config is 4 lines.

## Acceptance Criteria

1. `validate_algo_config` accepts a top-level `"profile"` key:
   `"conservative"` | `"moderate"` | `"aggressive"` | `"custom"`.

2. Each profile pre-fills all sub-block defaults:

   | Field | conservative | moderate | aggressive |
   |-------|-------------|----------|------------|
   | `activation_threshold` | 0.75 | 0.60 | 0.45 |
   | `max_drawdown_pct` | 0.10 | 0.15 | 0.25 |
   | `entry_quality_threshold` | 0.65 | 0.50 | 0.30 |
   | `stake_allocation_pct` | 0.25 | 0.50 | 0.80 |
   | `pair_selection.enabled` | true | true | false |

3. A minimal valid config:
   ```json
   "algo_system": {
       "profile": "moderate",
       "modules_path": "user_data/strategies/algo_system/modules",
       "active_modules": ["grid_trading_v1"],
       "default_module": "grid_trading_v1"
   }
   ```

4. Any key present in the user config **overrides** the profile default
   (profiles are additive, not replacing).

5. `validate_algo_config` logs the effective profile at INFO level so the
   operator can see exactly what defaults were applied.

6. `"custom"` profile applies no defaults beyond the existing hardcoded ones
   (backwards-compatible with all existing configs).

## Out of Scope

- UI/dashboard for config editing
- Hot-reload of config without bot restart

## Technical Notes

- Profile constants live in `config/algo_config_schema.py` as a dict-of-dicts.
- No new required keys introduced — all existing configs continue to work
  (they implicitly use the `"custom"` profile).

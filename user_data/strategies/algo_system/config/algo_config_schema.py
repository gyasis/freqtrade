"""
config/algo_config_schema.py
TypedDict schemas and validation for the algo_system config block.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("algo_system.config")

_REASONING_DEFAULTS = {
    "engine": "rule_based",
    "activation_threshold": 0.6,
    "routing_ttl_candles": 10,
    "adx_ranging_threshold": 25.0,
    "bb_width_ranging_threshold": 0.04,
}

_CB_DEFAULTS = {
    "max_drawdown_pct": 0.15,
    "price_move_pct": 0.08,
    "price_move_candles": 3,
    "cooling_candles": 10,
}

_EQ_DEFAULTS = {
    "momentum_lookback_candles": 10,
    "momentum_threshold": 0.03,
    "entry_quality_threshold": 0.5,
    "max_defer_candles": 20,
}

_PS_DEFAULTS = {
    "enabled": True,
    "evaluation_interval_candles": 24,
    "criteria": {},
}


def validate_algo_config(raw: dict) -> dict:
    """
    Validate and fill defaults for the algo_system config block.
    Raises ValueError for missing required fields or invalid values.
    Returns fully populated config dict.
    """
    cfg = dict(raw)

    # Required fields
    for key in ("modules_path", "active_modules", "default_module"):
        if key not in cfg:
            raise ValueError(f"algo_system config missing required key: '{key}'")

    if not cfg["active_modules"] or not isinstance(cfg["active_modules"], list):
        raise ValueError("algo_system.active_modules must be a non-empty list")

    if cfg["default_module"] not in cfg["active_modules"]:
        raise ValueError(
            f"algo_system.default_module '{cfg['default_module']}' "
            f"not in active_modules {cfg['active_modules']}"
        )

    cfg.setdefault("module_switch_cooldown_candles", 5)
    cfg.setdefault("modules", {})

    # Reasoning
    reasoning = dict(_REASONING_DEFAULTS)
    reasoning.update(cfg.get("reasoning", {}))
    _validate_range(reasoning, "activation_threshold", 0.0, 1.0)
    cfg["reasoning"] = reasoning

    # Circuit breaker
    cb = dict(_CB_DEFAULTS)
    cb.update(cfg.get("circuit_breaker", {}))
    _validate_range(cb, "max_drawdown_pct", 0.0, 1.0)
    _validate_range(cb, "price_move_pct", 0.0, 1.0)
    cfg["circuit_breaker"] = cb

    # Shared state
    ss = {"persistence_path": "user_data/algo_system_state.json"}
    ss.update(cfg.get("shared_state", {}))
    cfg["shared_state"] = ss

    # Observability
    obs = {"log_routing_changes": True, "log_signal_conflicts": True}
    obs.update(cfg.get("observability", {}))
    cfg["observability"] = obs

    # Entry quality
    eq = dict(_EQ_DEFAULTS)
    eq.update(cfg.get("entry_quality", {}))
    cfg["entry_quality"] = eq

    # Pair selection
    ps = dict(_PS_DEFAULTS)
    ps.update(cfg.get("pair_selection", {}))
    ps.setdefault("criteria", {})
    cfg["pair_selection"] = ps

    return cfg


def _validate_range(d: dict, key: str, lo: float, hi: float) -> None:
    v = d.get(key)
    if v is not None and not (lo <= v <= hi):
        raise ValueError(f"algo_system config: {key}={v} must be between {lo} and {hi}")

"""
observability/module_logger.py
Namespaced per-module logger factory for LATS.
"""
from __future__ import annotations

import logging
from typing import Optional

ROOT_LOGGER_NAME = "algo_system"


def get_module_logger(module_id: str, pair: Optional[str] = None) -> logging.Logger:
    """
    Return logger namespaced to module and optional pair.
    Pair slashes replaced with underscores for valid logger naming.
    """
    name = f"{ROOT_LOGGER_NAME}.{module_id}"
    if pair:
        safe_pair = pair.replace("/", "_").replace(":", "_")
        name = f"{name}.{safe_pair}"
    return logging.getLogger(name)


def get_system_logger(component: str) -> logging.Logger:
    """Return logger for a system component (orchestrator, circuit_breaker, etc.)."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{component}")


def configure_algo_system_logging(level: int = logging.INFO) -> None:
    """Configure root algo_system logger for testing. Freqtrade config takes precedence in prod."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        root.addHandler(handler)
    root.setLevel(level)

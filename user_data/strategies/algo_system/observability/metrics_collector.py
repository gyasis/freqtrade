"""
observability/metrics_collector.py
Routes system alerts to freqtrade's RPC/Telegram via dp.send_msg().
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("algo_system.metrics")


class MetricsCollector:
    """
    Wraps freqtrade's DataProvider.send_msg() to deliver structured alerts.

    All alerts route through freqtrade's existing Telegram/webhook RPC
    by calling dp.send_msg(msg, always_send=True).
    """

    def __init__(self, dp: Any) -> None:
        """dp: freqtrade DataProvider instance (may be None in backtesting)."""
        self._dp = dp

    def _send(self, message: str) -> None:
        """Send message via dp.send_msg if available."""
        logger.info("[ALERT] %s", message)
        if self._dp is None:
            return
        try:
            # send_msg is available in freqtrade's DataProvider for live/paper modes
            if hasattr(self._dp, "send_msg"):
                self._dp.send_msg({"type": "custom", "msg": message}, always_send=True)
        except Exception as exc:
            logger.warning("MetricsCollector: failed to send alert: %s", exc)

    def send_suspension_alert(
        self, module_id: str, pair: str, reason: str
    ) -> None:
        msg = (
            f"LATS ALERT: Module '{module_id}' SUSPENDED on {pair}\n"
            f"Reason: {reason}\n"
            f"Action: Manual review required. Use bot restart to reset."
        )
        self._send(msg)

    def send_circuit_breaker_alert(
        self, reason: str, drawdown_pct: Optional[float] = None
    ) -> None:
        dd_str = f" (drawdown={drawdown_pct:.1%})" if drawdown_pct is not None else ""
        msg = (
            f"LATS CIRCUIT BREAKER TRIPPED{dd_str}\n"
            f"Reason: {reason}\n"
            f"Action: All new entries halted. Existing stoplosses intact."
        )
        self._send(msg)

    def send_routing_change(
        self,
        from_module: Optional[str],
        to_module: str,
        pair: str,
        rationale: str,
    ) -> None:
        msg = (
            f"LATS Routing change on {pair}:\n"
            f"{from_module} -> {to_module}\n"
            f"Reason: {rationale}"
        )
        self._send(msg)

    def send_info(self, message: str) -> None:
        """General-purpose informational alert."""
        self._send(f"LATS: {message}")

    def send_alert(self, module_id: str, pair: str, message: str) -> None:
        """
        Send a module-level trading signal alert (used by alert-mode modules).

        Parameters
        ----------
        module_id:
            Identifier of the module producing the alert.
        pair:
            Trading pair the alert relates to (e.g. ``"BTC/USDT"``).
        message:
            Human-readable alert body; may be multi-line.
        """
        msg = (
            f"LATS SIGNAL ALERT [{module_id}] {pair}\n"
            f"{message}"
        )
        self._send(msg)

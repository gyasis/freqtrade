"""
T007 - Circuit Breaker: Safety halt mechanism for the LATS algo system.

Monitors portfolio drawdown and sudden price moves. When thresholds are
breached the breaker trips (TRIPPED state), blocking new orders. After a
configurable cooling period it transitions back to ARMED.

State machine:
    ARMED  --[threshold breached]--> TRIPPED
    TRIPPED  (stays TRIPPED until external reset or cooling logic)
    COOLING  --[counter reaches 0]--> ARMED
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pandas import DataFrame

logger = logging.getLogger("algo_system.circuit_breaker")


# ---------------------------------------------------------------------------
# Enums & dataclasses (as specified in data-model.md)
# ---------------------------------------------------------------------------


class CircuitBreakerStatus(str, Enum):
    ARMED = "armed"      # monitoring, ready to trigger
    TRIPPED = "tripped"  # halt state, no new orders
    COOLING = "cooling"  # recovering, monitoring for reset


@dataclass
class CircuitBreakerState:
    status: CircuitBreakerStatus = CircuitBreakerStatus.ARMED
    tripped_at: Optional[datetime] = None
    trip_reason: Optional[str] = None
    max_drawdown_pct: float = 0.15      # 15% portfolio drawdown triggers halt
    price_move_pct: float = 0.08        # 8% price move triggers halt
    price_move_candles: int = 3         # within 3 candles
    cooling_candles: int = 10           # candles to wait before returning to ARMED

    def is_active(self) -> bool:
        return self.status == CircuitBreakerStatus.TRIPPED


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """
    Evaluates safety conditions each candle and halts trading when thresholds
    are exceeded.

    Two independent trip conditions:
      1. Portfolio drawdown >= ``max_drawdown_pct``
      2. Absolute close price move over the last ``price_move_candles`` candles
         >= ``price_move_pct``

    After tripping the breaker enters TRIPPED state (all trade signals blocked).
    Caller must invoke ``begin_cooling()`` to start the recovery countdown; once
    ``cooling_candles`` candles have elapsed the breaker resets to ARMED
    automatically via ``evaluate()``.
    """

    def __init__(self, config: dict) -> None:
        """
        Initialise the circuit breaker from a configuration dictionary.

        Parameters
        ----------
        config:
            Recognised keys and their defaults:
            - ``max_drawdown_pct``   (float) : 0.15
            - ``price_move_pct``     (float) : 0.08
            - ``price_move_candles`` (int)   : 3
            - ``cooling_candles``    (int)   : 10
        """
        self.state: CircuitBreakerState = CircuitBreakerState(
            max_drawdown_pct=float(config.get("max_drawdown_pct", 0.15)),
            price_move_pct=float(config.get("price_move_pct", 0.08)),
            price_move_candles=int(config.get("price_move_candles", 3)),
            cooling_candles=int(config.get("cooling_candles", 10)),
        )
        self._cooling_counter: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(self, df: DataFrame, portfolio_drawdown_pct: float) -> bool:
        """
        Evaluate safety conditions for the current candle.

        Should be called once per closed candle. Returns ``True`` when the
        circuit breaker is active (trading should be halted), ``False`` when
        trading may proceed.

        Behaviour by state
        ------------------
        TRIPPED:
            Returns ``True`` immediately — the breaker stays tripped until
            ``begin_cooling()`` is called externally.
        COOLING:
            Decrements ``_cooling_counter`` by one each call. When the counter
            reaches zero the breaker resets to ARMED and returns ``False``.
            While counter > 0 returns ``False`` (trading is allowed during
            the recovery window — caller may choose to restrict entries
            independently based on ``state.status``).
        ARMED:
            Checks both thresholds. If either is breached, calls
            ``self.trip(reason)`` and returns ``True``. Otherwise returns
            ``False``.

        Parameters
        ----------
        df:
            OHLCV DataFrame for the current pair. Must contain a ``close``
            column. At least ``price_move_candles`` rows are needed for the
            price move check; if fewer rows are present the check is skipped.
        portfolio_drawdown_pct:
            Current peak-to-trough drawdown of the portfolio expressed as a
            positive fraction (e.g. 0.12 means 12 % drawdown).

        Returns
        -------
        bool
            ``True`` if trading should be halted, ``False`` otherwise.
        """
        if self.state.status == CircuitBreakerStatus.TRIPPED:
            logger.debug(
                "Circuit breaker already TRIPPED (reason=%s, tripped_at=%s); "
                "blocking evaluation.",
                self.state.trip_reason,
                self.state.tripped_at,
            )
            return True

        if self.state.status == CircuitBreakerStatus.COOLING:
            self._cooling_counter -= 1
            logger.debug(
                "Circuit breaker COOLING — candles remaining: %d",
                self._cooling_counter,
            )
            if self._cooling_counter <= 0:
                self.reset()
                logger.info("Circuit breaker cooling complete — resetting to ARMED.")
            return False

        # ---- ARMED state: check thresholds --------------------------------
        assert self.state.status == CircuitBreakerStatus.ARMED

        # Check 1: portfolio drawdown
        if portfolio_drawdown_pct >= self.state.max_drawdown_pct:
            reason = (
                f"Portfolio drawdown {portfolio_drawdown_pct:.2%} exceeded "
                f"threshold {self.state.max_drawdown_pct:.2%}"
            )
            logger.warning("Circuit breaker tripping — %s", reason)
            self.trip(reason)
            return True

        # Check 2: rapid price move over last N candles
        n = self.state.price_move_candles
        if len(df) >= n:
            close = df["close"]
            price_old = float(close.iloc[-n])
            price_new = float(close.iloc[-1])

            if price_old != 0.0:
                move = abs(price_new - price_old) / price_old
                if move >= self.state.price_move_pct:
                    reason = (
                        f"Price moved {move:.2%} over {n} candles "
                        f"(threshold {self.state.price_move_pct:.2%}): "
                        f"{price_old:.6g} -> {price_new:.6g}"
                    )
                    logger.warning("Circuit breaker tripping — %s", reason)
                    self.trip(reason)
                    return True
            else:
                logger.warning(
                    "Cannot compute price move: close[-%d] is zero; "
                    "skipping price move check.",
                    n,
                )
        else:
            logger.debug(
                "Insufficient candle history for price move check "
                "(have %d rows, need %d); skipping.",
                len(df),
                n,
            )

        return False

    def trip(self, reason: str) -> None:
        """
        Transition the circuit breaker to TRIPPED state.

        Parameters
        ----------
        reason:
            Human-readable description of why the breaker was tripped.
            Stored in ``state.trip_reason`` for observability.
        """
        self.state.status = CircuitBreakerStatus.TRIPPED
        self.state.tripped_at = datetime.now(tz=timezone.utc)
        self.state.trip_reason = reason
        self._cooling_counter = 0
        logger.error(
            "CIRCUIT BREAKER TRIPPED at %s — %s",
            self.state.tripped_at.isoformat(),
            reason,
        )

    def begin_cooling(self) -> None:
        """
        Transition from TRIPPED to COOLING and start the countdown.

        Should be called by the strategy once it has acknowledged a trip and
        is ready to begin recovery monitoring. The cooling counter is
        initialised to ``state.cooling_candles``; ``evaluate()`` decrements it
        each candle until it reaches zero, then resets to ARMED automatically.

        Has no effect if the breaker is not currently TRIPPED.
        """
        if self.state.status != CircuitBreakerStatus.TRIPPED:
            logger.debug(
                "begin_cooling() called but breaker is not TRIPPED (status=%s); "
                "ignoring.",
                self.state.status,
            )
            return
        self.state.status = CircuitBreakerStatus.COOLING
        self._cooling_counter = self.state.cooling_candles
        logger.info(
            "Circuit breaker entering COOLING state — %d candles until ARMED.",
            self._cooling_counter,
        )

    def reset(self) -> None:
        """
        Reset the circuit breaker to ARMED state unconditionally.

        Called internally when the cooling countdown completes. May also be
        called externally for manual recovery (e.g. operator override).
        Clears trip metadata and resets the cooling counter.
        """
        self.state.status = CircuitBreakerStatus.ARMED
        self.state.tripped_at = None
        self.state.trip_reason = None
        self._cooling_counter = 0
        logger.info("Circuit breaker RESET — status: ARMED.")

    def is_tripped(self) -> bool:
        """Return ``True`` if the circuit breaker is in TRIPPED state."""
        return self.state.is_active()

    def get_status_dict(self) -> dict:
        """
        Return a JSON-serialisable snapshot of the current breaker state.

        Suitable for logging, monitoring dashboards, or persistence.

        Returns
        -------
        dict
            Keys: ``status``, ``tripped_at``, ``trip_reason``,
            ``cooling_counter``, ``max_drawdown_pct``, ``price_move_pct``,
            ``price_move_candles``, ``cooling_candles``.
        """
        tripped_at_iso: Optional[str] = (
            self.state.tripped_at.isoformat()
            if self.state.tripped_at is not None
            else None
        )
        return {
            "status": self.state.status.value,
            "tripped_at": tripped_at_iso,
            "trip_reason": self.state.trip_reason,
            "cooling_counter": self._cooling_counter,
            "max_drawdown_pct": self.state.max_drawdown_pct,
            "price_move_pct": self.state.price_move_pct,
            "price_move_candles": self.state.price_move_candles,
            "cooling_candles": self.state.cooling_candles,
        }

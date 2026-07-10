"""Circuit breaker for DB / Redis — prevents cascading failure when downstream is down.

State machine:
  CLOSED   → normal operation, failures increment counter
  OPEN     → calls fail fast, recovery timer starts
  HALF_OPEN → probe request, success → CLOSED, failure → OPEN

Usage:
    breaker = CircuitBreaker("postgres", failure_threshold=5, recovery_timeout=30)
    with breaker:
        db_session.commit()
"""
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

logger = logging.getLogger("trafficflow.circuit_breaker")


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is open."""


class CircuitBreaker:
    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self.state = "CLOSED"
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.half_open_calls = 0

    def _reset(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"
        self.half_open_calls = 0

    def _on_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            logger.warning("CircuitBreaker[%s]: OPEN (failures=%d)", self.name, self.failure_count)
            self.state = "OPEN"
            self.half_open_calls = 0

    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        now = time.monotonic()

        if self.state == "OPEN":
            if now - self.last_failure_time >= self.recovery_timeout:
                logger.info("CircuitBreaker[%s]: HALF_OPEN (probing)", self.name)
                self.state = "HALF_OPEN"
                self.half_open_calls = 0
            else:
                raise CircuitBreakerOpen(f"CircuitBreaker[{self.name}] is OPEN")

        if self.state == "HALF_OPEN":
            if self.half_open_calls >= self.half_open_max_calls:
                raise CircuitBreakerOpen(f"CircuitBreaker[{self.name}] HALF_OPEN (max probes)")
            self.half_open_calls += 1

        try:
            result = fn(*args, **kwargs)
            if self.state == "HALF_OPEN":
                logger.info("CircuitBreaker[%s]: CLOSED (probe succeeded)", self.name)
                self._reset()
            else:
                self.failure_count = 0
            return result
        except Exception:
            self._on_failure()
            raise

    def __enter__(self) -> "CircuitBreaker":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None and exc_type is not CircuitBreakerOpen:
            self._on_failure()
        elif exc_type is None:
            self.failure_count = 0

    def __call__(self, fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(fn, *args, **kwargs)
        return wrapper


# Global singleton breakers
db_breaker = CircuitBreaker("postgres", failure_threshold=5, recovery_timeout=30)
redis_breaker = CircuitBreaker("redis", failure_threshold=3, recovery_timeout=15)

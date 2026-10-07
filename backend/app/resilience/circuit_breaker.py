"""Circuit breaker used around natural-language SQL generation."""

import time

from app.config import settings


class CircuitBreaker:
    """Opens after repeated failures and allows a trial call once the timeout passes."""

    def __init__(self, failure_threshold: int, recovery_timeout: float) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = "closed"
        self.opened_at: float | None = None

    def allow_request(self) -> bool:
        """Return False while the breaker is open and the recovery window has not elapsed."""
        if self.state == "closed":
            return True
        if self.state == "open":
            if (
                self.opened_at is not None
                and time.monotonic() - self.opened_at >= self.recovery_timeout
            ):
                self.state = "half_open"
                return True
            return False
        return True

    def record_success(self) -> None:
        """Close the breaker after a successful call."""
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self) -> None:
        """Count a failure and open the breaker once the threshold is reached."""
        self.failure_count += 1
        if self.state == "half_open" or self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, int | float | str | None]:
        return {
            "state": self.state,
            "failureCount": self.failure_count,
            "failureThreshold": self.failure_threshold,
            "recoverySeconds": self.recovery_timeout,
        }


llm_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.circuit_breaker_threshold,
    recovery_timeout=settings.circuit_breaker_recovery_seconds,
)

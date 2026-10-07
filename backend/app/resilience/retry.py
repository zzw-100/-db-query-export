"""Exponential backoff for transient failures on the request path."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import settings

logger = logging.getLogger(__name__)


async def retry_async(
    operation: Callable[[], Awaitable],
    *,
    retry_on: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, OSError),
):
    """Run an async operation, backing off between transient failures.

    Validation errors and programming errors propagate on the first failure.
    Timeout, connection, and OS errors wait `retry_base_delay_seconds`, then
    twice that, until `retry_max_attempts` is exhausted.
    """
    attempts = settings.retry_max_attempts
    base_delay = settings.retry_base_delay_seconds
    max_delay = settings.retry_max_delay_seconds
    if attempts < 1:
        attempts = 1

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts:
                logger.warning(
                    "Operation failed after %s attempts: %s", attempt, exc
                )
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            logger.warning(
                "Transient failure on attempt %s/%s, retrying in %.3fs: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            if delay > 0:
                await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error
    raise RuntimeError("retry_async exited without a result")

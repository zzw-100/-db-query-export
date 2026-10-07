"""Concurrent rate limits for query execution and SQL generation."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import settings


class RateLimitExceeded(Exception):
    """Raised when a request cannot obtain a concurrency slot in time."""


class RateLimiter:
    """Caps how many operations of one kind may run at the same time."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.active_count = 0
        self.total_requests = 0
        self.total_rejections = 0

    @asynccontextmanager
    async def slot(self, *, timeout: float) -> AsyncIterator[None]:
        """Hold one concurrency slot until the block exits."""
        self.total_requests += 1
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout)
        except TimeoutError as exc:
            self.total_rejections += 1
            raise RateLimitExceeded(
                f"Too many concurrent operations (limit {self.max_concurrent})"
            ) from exc

        self.active_count += 1
        try:
            yield
        finally:
            self.active_count -= 1
            self._semaphore.release()

    def stats(self) -> dict[str, int]:
        """Return counters for the metrics endpoint."""
        return {
            "maxConcurrent": self.max_concurrent,
            "active": self.active_count,
            "totalRequests": self.total_requests,
            "rejected": self.total_rejections,
        }


class RateLimiters:
    """Query and LLM limiters constructed from settings."""

    def __init__(self) -> None:
        self.queries = RateLimiter(settings.rate_limit_query_concurrency)
        self.llm = RateLimiter(settings.rate_limit_llm_concurrency)

    def stats(self) -> dict[str, dict[str, int]]:
        return {"queries": self.queries.stats(), "llm": self.llm.stats()}


rate_limiters = RateLimiters()

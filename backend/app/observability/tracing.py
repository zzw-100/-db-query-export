"""Request ids and short spans carried through a single request."""

import contextvars
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.observability.metrics import metrics

logger = logging.getLogger(__name__)

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def current_request_id() -> str:
    """Return the id bound by the HTTP middleware, or '-' outside a request."""
    return request_id_var.get()


@contextmanager
def trace_span(name: str, **attributes: str) -> Iterator[None]:
    """Time one step of the request and record it on the metrics collector."""
    started = time.perf_counter()
    request_id = current_request_id()
    logger.info(
        "span start %s request_id=%s %s",
        name,
        request_id,
        " ".join(f"{key}={value}" for key, value in attributes.items()),
    )
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.observe(f"{name}_ms", elapsed_ms, **attributes)
        logger.info(
            "span end %s request_id=%s elapsed_ms=%.1f",
            name,
            request_id,
            elapsed_ms,
        )

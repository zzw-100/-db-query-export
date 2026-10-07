"""FastAPI application entry point."""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.registry import adapter_registry
from app.api.v1 import databases, queries
from app.config import settings
from app.database import init_db
from app.observability.logging_setup import configure_logging
from app.observability.metrics import metrics
from app.observability.tracing import request_id_var
from app.resilience.circuit_breaker import llm_circuit_breaker
from app.resilience.rate_limiter import rate_limiters
from app.services.db_connection import close_all_connection_pools

configure_logging()

# Initialize database
init_db()

# Create FastAPI app
app = FastAPI(
    title="Database Query Tool API",
    description="REST API for managing PostgreSQL database connections and executing queries",
    version="1.0.0",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(databases.router)
app.include_router(queries.router)

logger = logging.getLogger(__name__)


@app.middleware("http")
async def bind_request_context(request: Request, call_next):
    """Attach a request id and record HTTP metrics for every request."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        metrics.increment("http_errors_total", path=request.url.path)
        raise
    else:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.increment(
            "http_requests_total",
            method=request.method,
            status=str(response.status_code),
        )
        metrics.observe(
            "http_request_ms",
            elapsed_ms,
            method=request.method,
            path=request.url.path,
        )
        response.headers["x-request-id"] = request_id
        return response
    finally:
        request_id_var.reset(token)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/api/v1/observability/metrics")
async def observability_metrics() -> dict:
    """Expose the counters, rate limiter, and circuit breaker used by requests."""
    return {
        "metrics": metrics.snapshot(),
        "rateLimiter": rate_limiters.stats(),
        "circuitBreaker": llm_circuit_breaker.snapshot(),
    }


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize database on startup and apply the configured log level."""
    configure_logging()
    init_db()
    logger.info("Server started with log_level=%s", settings.log_level)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Cleanup resources on shutdown."""
    await close_all_connection_pools()
    await adapter_registry.close_all_adapters()

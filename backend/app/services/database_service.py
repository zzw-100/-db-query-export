"""High-level database service (Facade pattern)."""

import logging
import time
import uuid
from typing import Optional, Tuple

from app.adapters.base import MetadataResult, QueryResult
from app.adapters.registry import DatabaseAdapterRegistry, adapter_registry
from app.config import settings
from app.models.database import DatabaseType
from app.observability.metrics import metrics
from app.observability.tracing import current_request_id, trace_span
from app.resilience.rate_limiter import RateLimitExceeded, rate_limiters
from app.services.access_policy import AccessPolicy, policy_from_settings
from app.services.executor_registry import WrongDatabaseError, executor_registry
from app.services.sql_validator import (
    AccessDeniedError,
    SqlValidationError,
    validate_and_transform_sql,
)

logger = logging.getLogger(__name__)


class DatabaseService:
    """High-level service for database operations (Facade pattern).

    This class provides a simplified interface to database operations,
    coordinating between adapters, validators, and other components.

    Example:
        service = DatabaseService(adapter_registry)
        result = await service.execute_query(
            DatabaseType.POSTGRESQL,
            "mydb",
            "postgresql://...",
            "SELECT * FROM users"
        )
    """

    def __init__(self, registry: DatabaseAdapterRegistry, executors=None):
        """Initialize service with adapter registry.

        Args:
            registry: Database adapter registry
            executors: Per-database executor registry. Defaults to the process-wide one.
        """
        self.registry = registry
        self.executors = executors if executors is not None else executor_registry
        logger.info("Initialized DatabaseService")

    async def test_connection(
        self, db_type: DatabaseType, url: str
    ) -> Tuple[bool, Optional[str]]:
        """Test database connection.

        Each probe gets its own adapter. Probes are not cached under a shared
        name, so testing one URL cannot leave the next probe on the wrong database.

        Args:
            db_type: Database type
            url: Connection URL

        Returns:
            Tuple of (success, error_message)
        """
        config = settings.connection_config(url, f"probe-{uuid.uuid4().hex}")
        adapter = self.registry.create_ephemeral(db_type, config)
        try:
            return await adapter.test_connection()
        finally:
            await adapter.close_connection_pool()

    async def execute_query(
        self,
        db_type: DatabaseType,
        name: str,
        url: str,
        sql: str,
        limit: int | None = None,
        policy: AccessPolicy | None = None,
    ) -> Tuple[QueryResult, int]:
        """Execute SQL query.

        Args:
            db_type: Database type
            name: Connection name
            url: Connection URL
            sql: SQL query (will be validated)
            limit: Maximum rows to return

        Returns:
            Tuple of (QueryResult, execution_time_ms)

        Raises:
            SqlValidationError: If SQL is invalid
            AccessDeniedError: If the access policy rejects the SQL
            RateLimitExceeded: If too many queries are already running
            WrongDatabaseError: If the executor is not bound to this database
            Exception: If query execution fails
        """
        active_policy = policy if policy is not None else policy_from_settings()
        row_limit = settings.query_default_limit if limit is None else limit
        request_id = current_request_id()

        with trace_span("db.query", database=name):
            try:
                validated_sql = validate_and_transform_sql(
                    sql,
                    limit=row_limit,
                    db_type=db_type,
                    policy=active_policy,
                )
                async with rate_limiters.queries.slot(
                    timeout=settings.rate_limit_acquire_timeout
                ):
                    metrics.increment("db_query_total", database=name)
                    start_time = time.perf_counter()
                    result = await self.executors.execute_validated(
                        name=name,
                        db_type=db_type,
                        url=url,
                        sql=validated_sql,
                    )
                    execution_time_ms = int((time.perf_counter() - start_time) * 1000)
                    metrics.observe(
                        "db_query_latency_ms", execution_time_ms, database=name
                    )
                    logger.info(
                        "Query executed on %s request_id=%s rows=%s elapsed_ms=%s",
                        name,
                        request_id,
                        result.row_count,
                        execution_time_ms,
                    )
                    return result, execution_time_ms
            except (AccessDeniedError, SqlValidationError):
                metrics.increment("sql_rejected_total", database=name)
                raise
            except RateLimitExceeded:
                metrics.increment("rate_limited_total", database=name)
                raise
            except WrongDatabaseError:
                metrics.increment("wrong_database_total", database=name)
                raise
            except Exception as exc:
                metrics.increment("db_query_errors_total", database=name)
                logger.error(
                    "Query failed on %s request_id=%s: %s", name, request_id, exc
                )
                raise

    async def extract_metadata(
        self,
        db_type: DatabaseType,
        name: str,
        url: str,
    ) -> MetadataResult:
        """Extract database metadata.

        Args:
            db_type: Database type
            name: Connection name
            url: Connection URL

        Returns:
            MetadataResult

        Example:
            metadata = await service.extract_metadata(
                DatabaseType.POSTGRESQL,
                "mydb",
                "postgresql://..."
            )
        """
        config = settings.connection_config(url, name)
        adapter = await self.registry.rebind(db_type, config)
        if adapter.config.name != name or adapter.config.url != url:
            raise WrongDatabaseError(
                f"Refusing to read metadata for '{name}' from a different database"
            )

        logger.info(f"Extracting metadata for {name}")
        metadata = await adapter.extract_metadata()
        logger.info(
            f"Extracted metadata for {name}: "
            f"{len(metadata.tables)} tables, {len(metadata.views)} views"
        )

        return metadata

    async def close_connection(
        self,
        db_type: DatabaseType,
        name: str,
    ) -> None:
        """Close database connection.

        Args:
            db_type: Database type
            name: Connection name
        """
        await self.registry.close_adapter(db_type, name)
        self.executors.forget(name)
        logger.info(f"Closed connection for {name}")


# Global service instance
database_service = DatabaseService(adapter_registry)

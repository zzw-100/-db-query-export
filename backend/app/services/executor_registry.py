"""Per-database executors.

Each request names a database and is bound to the adapter for that name and
URL. There is no default executor. A cached adapter whose URL no longer
matches the registered connection is closed and replaced before any SQL runs.
"""

import logging
from dataclasses import dataclass

from app.adapters.base import DatabaseAdapter, QueryResult
from app.adapters.registry import DatabaseAdapterRegistry, adapter_registry
from app.config import settings
from app.models.database import DatabaseType
from app.resilience.retry import retry_async

logger = logging.getLogger(__name__)


class WrongDatabaseError(Exception):
    """Raised when the executor that would run the SQL is not the requested database."""


@dataclass
class BoundExecutor:
    """An adapter pinned to one connection identity."""

    name: str
    db_type: DatabaseType
    url: str
    adapter: DatabaseAdapter


class ExecutorRegistry:
    """Resolves the executor for the database named by the request."""

    def __init__(self, adapters: DatabaseAdapterRegistry | None = None) -> None:
        self._adapters = adapters if adapters is not None else adapter_registry
        self._bound: dict[str, BoundExecutor] = {}

    async def resolve(self, name: str, db_type: DatabaseType, url: str) -> BoundExecutor:
        """Return the executor bound to this name and URL, rebinding if the URL changed."""
        if not name:
            raise WrongDatabaseError("Database name is required")

        config = settings.connection_config(url, name)
        adapter = await self._adapters.rebind(db_type, config)
        if adapter.config.name != name or adapter.config.url != url:
            raise WrongDatabaseError(
                f"Refusing to run SQL for '{name}' on executor "
                f"'{adapter.config.name}' ({adapter.config.url})"
            )

        bound = BoundExecutor(name=name, db_type=db_type, url=url, adapter=adapter)
        previous = self._bound.get(name)
        if previous is not None and previous.url != url:
            logger.warning(
                "Rebound executor for '%s' because the connection URL changed", name
            )
        self._bound[name] = bound
        return bound

    async def execute_validated(
        self,
        *,
        name: str,
        db_type: DatabaseType,
        url: str,
        sql: str,
    ) -> QueryResult:
        """Run already-validated SQL on the executor for this database only."""
        bound = await self.resolve(name, db_type, url)
        if bound.name != name or bound.url != url or bound.db_type != db_type:
            raise WrongDatabaseError(f"Executor identity mismatch for '{name}'")

        async def run() -> QueryResult:
            return await bound.adapter.execute_query(sql)

        return await retry_async(run)

    def forget(self, name: str) -> None:
        """Drop the binding after a connection is deleted."""
        self._bound.pop(name, None)


executor_registry = ExecutorRegistry(adapter_registry)

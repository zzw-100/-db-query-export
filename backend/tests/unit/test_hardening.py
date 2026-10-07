"""Tests for multi-database execution, access control, and request-path resilience."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.base import (
    ConnectionConfig,
    DatabaseAdapter,
    MetadataResult,
    QueryResult,
    result_payload,
)
from app.adapters.registry import DatabaseAdapterRegistry
from app.config import settings
from app.database import get_session
from app.main import app
from app.models.database import ConnectionStatus, DatabaseConnection, DatabaseType
from app.models.metadata import DatabaseMetadata
from app.models.query import QueryHistory, QuerySource
from app.observability.logging_setup import configure_logging
from app.observability.metrics import MetricsCollector
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.rate_limiter import RateLimitExceeded, RateLimiter
from app.resilience.retry import retry_async
from app.services.access_policy import AccessPolicy, merge_policy, policy_from_settings
from app.services.database_service import DatabaseService
from app.services.executor_registry import ExecutorRegistry, WrongDatabaseError
from app.services.query import cleanup_old_queries
from app.services.sql_validator import (
    AccessDeniedError,
    validate_and_transform_sql,
    validate_sql,
)


class TinyAdapter(DatabaseAdapter):
    """In-memory adapter used to prove executors are not shared."""

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.closed = False
        self.queries: list[str] = []

    async def test_connection(self):
        return True, None

    async def get_connection_pool(self):
        return None

    async def close_connection_pool(self) -> None:
        self.closed = True
        self._pool = None

    async def extract_metadata(self) -> MetadataResult:
        return MetadataResult(tables=[], views=[])

    async def execute_query(self, sql: str) -> QueryResult:
        self.queries.append(sql)
        return QueryResult(columns=[], rows=[], row_count=0)

    def get_dialect_name(self) -> str:
        return "postgres"

    def get_identifier_quote_char(self) -> str:
        return '"'


class RecordingRegistry:
    """Minimal adapter registry that keeps one adapter per database name."""

    def __init__(self) -> None:
        self.adapters: dict[str, TinyAdapter] = {}

    async def rebind(self, db_type: DatabaseType, config: ConnectionConfig) -> TinyAdapter:
        current = self.adapters.get(config.name)
        if current is None or current.config.url != config.url:
            if current is not None:
                await current.close_connection_pool()
            current = TinyAdapter(config)
            self.adapters[config.name] = current
        return current


class CrossWiredRegistry:
    """Always hands back one adapter, which is the bug the registry must reject."""

    def __init__(self, adapter: TinyAdapter) -> None:
        self.adapter = adapter

    async def rebind(self, db_type: DatabaseType, config: ConnectionConfig) -> TinyAdapter:
        return self.adapter


class FakeExecutors:
    def __init__(self) -> None:
        self.sql: str | None = None
        self.name: str | None = None

    async def execute_validated(self, *, name: str, db_type, url: str, sql: str) -> QueryResult:
        self.sql = sql
        self.name = name
        return QueryResult(
            columns=[{"name": "n", "dataType": "integer"}],
            rows=[{"n": 1}],
            row_count=1,
        )

    def forget(self, name: str) -> None:
        return None


def _open_policy(**overrides) -> AccessPolicy:
    base = policy_from_settings()
    data = {
        "blocked_tables": base.blocked_tables,
        "blocked_columns": base.blocked_columns,
        "blocked_functions": base.blocked_functions,
        "allow_explain": base.allow_explain,
    }
    data.update(overrides)
    return AccessPolicy(**data)


class TestAccessControl:
    def test_blocks_table_column_and_star(self):
        policy = _open_policy(
            blocked_tables=frozenset({"salaries"}),
            blocked_columns=frozenset({"ssn", "users.password"}),
        )
        ok, error = validate_sql("SELECT id FROM salaries", policy=policy)
        assert ok is False
        assert error is not None
        assert "salaries" in error

        ok, error = validate_sql("SELECT ssn FROM employees", policy=policy)
        assert ok is False
        assert "ssn" in error

        ok, error = validate_sql("SELECT password FROM users", policy=policy)
        assert ok is False
        assert "password" in error

        ok, error = validate_sql("SELECT * FROM employees", policy=policy)
        assert ok is False
        assert "SELECT *" in error

    def test_blocks_dangerous_function_and_multiple_statements(self):
        ok, error = validate_sql("SELECT pg_sleep(1)")
        assert ok is False
        assert error is not None
        assert "pg_sleep" in error

        ok, error = validate_sql("SELECT 1; DROP TABLE users")
        assert ok is False
        assert error is not None
        assert "Multiple statements" in error

    def test_explain_policy(self):
        denied, error = validate_sql("EXPLAIN SELECT id FROM users")
        assert denied is False
        assert error is not None
        assert "EXPLAIN" in error

        allowed = _open_policy(allow_explain=True)
        ok, error = validate_sql("EXPLAIN SELECT id FROM users", policy=allowed)
        assert ok is True
        assert error is None

        with pytest.raises(AccessDeniedError):
            validate_and_transform_sql(
                "EXPLAIN ANALYZE SELECT id FROM users",
                policy=allowed,
            )

        rewritten = validate_and_transform_sql(
            "EXPLAIN SELECT id FROM users",
            limit=5,
            policy=allowed,
        )
        assert rewritten.upper().startswith("EXPLAIN")
        assert "LIMIT" not in rewritten.upper()

    def test_global_denylist_cannot_be_removed(self, monkeypatch):
        monkeypatch.setattr(settings, "security_blocked_tables", "secrets")
        merged = merge_policy(None)
        assert "secrets" in merged.blocked_tables
        with pytest.raises(AccessDeniedError):
            validate_and_transform_sql("SELECT id FROM secrets", policy=merged)


class TestExecutorBinding:
    @pytest.mark.asyncio
    async def test_each_database_uses_its_own_executor(self):
        registry = ExecutorRegistry(RecordingRegistry())
        await registry.execute_validated(
            name="sales",
            db_type=DatabaseType.POSTGRESQL,
            url="postgresql://localhost/sales",
            sql="SELECT 1",
        )
        await registry.execute_validated(
            name="hr",
            db_type=DatabaseType.POSTGRESQL,
            url="postgresql://localhost/hr",
            sql="SELECT 2",
        )
        adapters = registry._adapters.adapters
        assert adapters["sales"].queries == ["SELECT 1"]
        assert adapters["hr"].queries == ["SELECT 2"]
        assert adapters["sales"].config.url.endswith("/sales")
        assert adapters["hr"].config.url.endswith("/hr")

    @pytest.mark.asyncio
    async def test_refuses_a_cross_wired_executor(self):
        foreign = TinyAdapter(ConnectionConfig(url="postgresql://localhost/other", name="other"))
        registry = ExecutorRegistry(CrossWiredRegistry(foreign))
        with pytest.raises(WrongDatabaseError):
            await registry.execute_validated(
                name="sales",
                db_type=DatabaseType.POSTGRESQL,
                url="postgresql://localhost/sales",
                sql="SELECT 1",
            )
        assert foreign.queries == []

    @pytest.mark.asyncio
    async def test_rebind_closes_executor_when_url_changes(self):
        registry = DatabaseAdapterRegistry()
        registry.register(DatabaseType.POSTGRESQL, TinyAdapter)
        first = await registry.rebind(
            DatabaseType.POSTGRESQL,
            ConnectionConfig(url="postgresql://localhost/old", name="demo"),
        )
        second = await registry.rebind(
            DatabaseType.POSTGRESQL,
            ConnectionConfig(url="postgresql://localhost/new", name="demo"),
        )
        assert first is not second
        assert first.closed is True
        assert second.config.url.endswith("/new")
        third = await registry.rebind(
            DatabaseType.POSTGRESQL,
            ConnectionConfig(url="postgresql://localhost/new", name="demo"),
        )
        assert third is second

    @pytest.mark.asyncio
    async def test_query_limit_and_rejection_use_the_request_executor(self, monkeypatch):
        monkeypatch.setattr(settings, "query_default_limit", 7)
        executors = FakeExecutors()
        service = DatabaseService(DatabaseAdapterRegistry(), executors)
        _result, _elapsed = await service.execute_query(
            DatabaseType.POSTGRESQL,
            "demo",
            "postgresql://localhost/demo",
            "SELECT 1",
        )
        assert executors.name == "demo"
        assert executors.sql is not None
        assert "LIMIT" in executors.sql.upper()
        assert "7" in executors.sql

        monkeypatch.setattr(settings, "security_blocked_tables", "salaries")
        blocked = FakeExecutors()
        blocked_service = DatabaseService(DatabaseAdapterRegistry(), blocked)
        with pytest.raises(AccessDeniedError):
            await blocked_service.execute_query(
                DatabaseType.POSTGRESQL,
                "demo",
                "postgresql://localhost/demo",
                "SELECT id FROM salaries",
            )
        assert blocked.sql is None


class TestResilienceAndObservability:
    @pytest.mark.asyncio
    async def test_rate_limiter_rejects_when_full(self):
        limiter = RateLimiter(1)
        async with limiter.slot(timeout=1):
            with pytest.raises(RateLimitExceeded):
                async with limiter.slot(timeout=0.05):
                    pass
        assert limiter.stats()["rejected"] == 1

    @pytest.mark.asyncio
    async def test_retry_backs_off_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(settings, "retry_max_attempts", 3)
        monkeypatch.setattr(settings, "retry_base_delay_seconds", 0)
        monkeypatch.setattr(settings, "retry_max_delay_seconds", 0)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("temporary")
            return "ok"

        assert await retry_async(flaky) == "ok"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_retry_does_not_hide_validation_errors(self):
        async def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await retry_async(boom)

    def test_circuit_breaker_opens_after_threshold(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        assert breaker.allow_request() is True
        breaker.record_failure()
        assert breaker.allow_request() is True
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.allow_request() is False
        breaker.record_success()
        assert breaker.allow_request() is True

    def test_metrics_follow_the_enabled_flag(self, monkeypatch):
        collector = MetricsCollector()
        monkeypatch.setattr(settings, "metrics_enabled", True)
        collector.increment("demo_total")
        collector.observe("demo_ms", 12)
        snapshot = collector.snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["counters"][0]["value"] == 1

        monkeypatch.setattr(settings, "metrics_enabled", False)
        collector.increment("demo_total")
        assert collector.snapshot()["enabled"] is False
        assert collector.snapshot()["counters"][0]["value"] == 1

    def test_logging_setup_reads_configured_level(self, monkeypatch):
        monkeypatch.setattr(settings, "log_level", "ERROR")
        assert configure_logging() == 40


class TestConfigIsApplied:
    def test_metadata_staleness_uses_configured_hours(self, monkeypatch):
        meta = DatabaseMetadata(
            database_name="demo",
            metadata_json="{}",
            fetched_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
            table_count=0,
        )
        monkeypatch.setattr(settings, "metadata_cache_hours", 1)
        assert meta.is_stale is True
        monkeypatch.setattr(settings, "metadata_cache_hours", 48)
        assert meta.is_stale is False

    def test_connection_pool_settings_are_copied_onto_the_executor(self, monkeypatch):
        monkeypatch.setattr(settings, "db_pool_min_size", 2)
        monkeypatch.setattr(settings, "db_pool_max_size", 4)
        monkeypatch.setattr(settings, "db_pool_command_timeout", 15)
        config = settings.connection_config("mysql://localhost/demo", "demo")
        assert config.min_pool_size == 2
        assert config.max_pool_size == 4
        assert config.command_timeout == 15
        assert config.name == "demo"

    @pytest.mark.asyncio
    async def test_history_retention_setting_is_enforced(self, monkeypatch):
        monkeypatch.setattr(settings, "query_history_retention", 2)
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            for index in range(5):
                session.add(
                    QueryHistory(
                        database_name="demo",
                        sql_text=f"SELECT {index}",
                        executed_at=datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(seconds=index),
                        execution_time_ms=1,
                        row_count=1,
                        success=True,
                        query_source=QuerySource.MANUAL,
                    )
                )
            session.commit()
            await cleanup_old_queries(session, "demo")
            remaining = session.exec(select(QueryHistory)).all()
            assert len(remaining) == 2


class TestSingleSerializer:
    def test_query_and_metadata_share_one_payload_function(self):
        assert not hasattr(QueryResult, "to_dict")
        assert not hasattr(MetadataResult, "to_dict")
        query_body = result_payload(QueryResult(columns=[], rows=[], row_count=3))
        meta_body = result_payload(MetadataResult(tables=[{"name": "users"}], views=[]))
        assert query_body == {"columns": [], "rows": [], "rowCount": 3}
        assert meta_body["tables"][0]["name"] == "users"


@pytest.fixture
def test_session():
    engine = create_engine(
        "sqlite:///file:policy_test?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    conn = DatabaseConnection(
        name="guarded_db",
        url="postgresql://user:pass@localhost/guarded",
        description="Guarded database",
        status=ConnectionStatus.ACTIVE,
        last_connected_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    test_session.add(conn)
    test_session.commit()
    return conn


class TestPolicyApi:
    def test_policy_blocks_query_before_execution(self, client, sample_connection):
        saved = client.put(
            "/api/v1/dbs/guarded_db/policy",
            json={
                "blockedTables": ["salaries"],
                "blockedColumns": ["ssn"],
                "allowExplain": False,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["databaseName"] == "guarded_db"
        assert "salaries" in body["blockedTables"]
        assert body["allowExplain"] is False
        assert "pg_sleep" in body["blockedFunctions"]

        denied = client.post(
            "/api/v1/dbs/guarded_db/query",
            json={"sql": "SELECT id FROM salaries"},
        )
        assert denied.status_code == 403
        assert "salaries" in denied.json()["detail"]

        explain = client.post(
            "/api/v1/dbs/guarded_db/query",
            json={"sql": "EXPLAIN SELECT 1"},
        )
        assert explain.status_code == 403

    def test_request_id_and_metrics_are_on_the_request_path(self, client):
        response = client.get("/health", headers={"x-request-id": "req-hardening"})
        assert response.status_code == 200
        assert response.headers["x-request-id"] == "req-hardening"

        metrics_response = client.get("/api/v1/observability/metrics")
        assert metrics_response.status_code == 200
        payload = metrics_response.json()
        assert payload["metrics"]["enabled"] is True
        assert "queries" in payload["rateLimiter"]
        assert payload["circuitBreaker"]["state"] in {"closed", "open", "half_open"}
        names = {item["name"] for item in payload["metrics"]["counters"]}
        assert "http_requests_total" in names

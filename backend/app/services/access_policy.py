"""Access policy resolution.

Global denylists come from settings. A per-database row can add more
blocked tables or columns and can turn EXPLAIN on for that database only.
Global blocks are never removed by a per-database row.
"""

from dataclasses import dataclass

from sqlmodel import Session

from app.config import settings
from app.models.access_policy import DatabaseAccessPolicy

DEFAULT_BLOCKED_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "pg_write_file",
        "sleep",
    }
)


def parse_name_list(value: str | None) -> frozenset[str]:
    """Split a comma-separated denylist into lowercase names."""
    if not value:
        return frozenset()
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def format_name_list(values: list[str] | frozenset[str]) -> str:
    """Store names in a stable comma-separated form."""
    cleaned = sorted({item.strip().lower() for item in values if item and item.strip()})
    return ",".join(cleaned)


@dataclass(frozen=True)
class AccessPolicy:
    """Rules enforced before a query is allowed to run."""

    blocked_tables: frozenset[str]
    blocked_columns: frozenset[str]
    blocked_functions: frozenset[str]
    allow_explain: bool

    def without_explain(self) -> "AccessPolicy":
        """Return a copy that rejects EXPLAIN, used when checking the inner query."""
        return AccessPolicy(
            blocked_tables=self.blocked_tables,
            blocked_columns=self.blocked_columns,
            blocked_functions=self.blocked_functions,
            allow_explain=False,
        )


def policy_from_settings() -> AccessPolicy:
    """Build the global policy. An empty function list falls back to the builtin set."""
    configured_functions = parse_name_list(settings.security_blocked_functions)
    return AccessPolicy(
        blocked_tables=parse_name_list(settings.security_blocked_tables),
        blocked_columns=parse_name_list(settings.security_blocked_columns),
        blocked_functions=configured_functions or DEFAULT_BLOCKED_FUNCTIONS,
        allow_explain=settings.security_allow_explain,
    )


def merge_policy(record: DatabaseAccessPolicy | None) -> AccessPolicy:
    """Combine the global policy with one database's stored overrides."""
    base = policy_from_settings()
    if record is None:
        return base
    return AccessPolicy(
        blocked_tables=base.blocked_tables | parse_name_list(record.blocked_tables),
        blocked_columns=base.blocked_columns | parse_name_list(record.blocked_columns),
        blocked_functions=base.blocked_functions,
        allow_explain=record.allow_explain,
    )


def load_access_policy(session: Session, database_name: str) -> AccessPolicy:
    """Load the effective policy for a registered database."""
    record = session.get(DatabaseAccessPolicy, database_name)
    return merge_policy(record)

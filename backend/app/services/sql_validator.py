"""SQL validation: read-only SELECT, access policy, and EXPLAIN rules."""

import re

import sqlglot
from sqlglot import exp

from app.models.database import DatabaseType
from app.services.access_policy import AccessPolicy, policy_from_settings

_EXPLAIN_OPTIONS = re.compile(r"^(?:\([^)]*\)\s*)+", re.IGNORECASE)
_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter)


class SqlValidationError(Exception):
    """Raised when SQL validation fails."""


class AccessDeniedError(SqlValidationError):
    """Raised when SQL is syntactically a read, but the access policy rejects it."""


def _dialect(db_type: DatabaseType) -> str:
    return "postgres" if db_type == DatabaseType.POSTGRESQL else "mysql"


def _resolve_policy(policy: AccessPolicy | None) -> AccessPolicy:
    return policy if policy is not None else policy_from_settings()


def _command_name(statement: exp.Expression) -> str:
    this = statement.args.get("this")
    if this is None:
        return ""
    return str(this).upper()


def _command_expression_text(statement: exp.Expression) -> str:
    expression = statement.args.get("expression")
    if expression is None:
        return ""
    if isinstance(expression, exp.Literal):
        return str(expression.this)
    return expression.sql() if hasattr(expression, "sql") else str(expression)


def _is_explain(statement: exp.Expression) -> bool:
    explain_type = getattr(exp, "Explain", None)
    if explain_type is not None and isinstance(statement, explain_type):
        return True
    return isinstance(statement, exp.Command) and _command_name(statement) == "EXPLAIN"


def _function_names(statement: exp.Expression) -> list[str]:
    names: list[str] = []
    for node in statement.walk():
        if isinstance(node, exp.Anonymous) and node.name:
            names.append(node.name.lower())
        elif isinstance(node, exp.Func) and not isinstance(node, exp.Anonymous):
            raw = node.sql_name() if hasattr(node, "sql_name") else ""
            if raw:
                names.append(raw.lower())
    return names


def _check_readonly_shape(statement: exp.Expression) -> None:
    """Reject writes anywhere in the tree, and require a top-level SELECT."""
    for node in statement.walk():
        if isinstance(node, _FORBIDDEN):
            raise SqlValidationError("Only SELECT statements are allowed")
    if not isinstance(statement, exp.Select):
        raise SqlValidationError("Only SELECT statements are allowed")


def _check_functions(statement: exp.Expression, policy: AccessPolicy) -> None:
    for name in _function_names(statement):
        if name in policy.blocked_functions:
            raise AccessDeniedError(f"Function '{name}' is blocked for security reasons")


def _check_tables(statement: exp.Expression, policy: AccessPolicy) -> None:
    if not policy.blocked_tables:
        return
    for table in statement.find_all(exp.Table):
        table_name = (table.name or "").lower()
        if not table_name:
            continue
        schema_name = (table.db or "").lower()
        qualified = f"{schema_name}.{table_name}" if schema_name else ""
        if table_name in policy.blocked_tables or qualified in policy.blocked_tables:
            raise AccessDeniedError(f"Access to table '{table_name}' is not allowed")


def _check_columns(statement: exp.Expression, policy: AccessPolicy) -> None:
    if not policy.blocked_columns:
        return
    if statement.find(exp.Star):
        raise AccessDeniedError(
            "SELECT * is not allowed while column access restrictions are in effect"
        )
    visible_tables = {
        (table.name or "").lower()
        for table in statement.find_all(exp.Table)
        if table.name
    }
    for column in statement.find_all(exp.Column):
        column_name = (column.name or "").lower()
        if not column_name:
            continue
        qualified = f"{column.table.lower()}.{column_name}" if column.table else ""
        inferred = {
            f"{table_name}.{column_name}" for table_name in visible_tables
        }
        if (
            column_name in policy.blocked_columns
            or qualified in policy.blocked_columns
            or inferred & policy.blocked_columns
        ):
            shown = qualified or column_name
            raise AccessDeniedError(f"Access to column '{shown}' is not allowed")


def _explain_inner_sql(statement: exp.Expression) -> str:
    inner = _command_expression_text(statement).strip()
    if inner.upper().startswith("ANALYZE"):
        raise AccessDeniedError(
            "EXPLAIN ANALYZE is not allowed because it executes the statement"
        )
    inner = _EXPLAIN_OPTIONS.sub("", inner).strip()
    if inner.upper().startswith("ANALYZE"):
        raise AccessDeniedError(
            "EXPLAIN ANALYZE is not allowed because it executes the statement"
        )
    if not inner:
        raise SqlValidationError("EXPLAIN is missing the statement to inspect")
    return inner


def _validate_explain(
    statement: exp.Expression,
    db_type: DatabaseType,
    policy: AccessPolicy,
) -> None:
    if not policy.allow_explain:
        raise AccessDeniedError("EXPLAIN statements are not allowed")
    inner_sql = _explain_inner_sql(statement)
    validate_or_raise(inner_sql, db_type, policy.without_explain())


def validate_or_raise(
    sql: str,
    db_type: DatabaseType = DatabaseType.POSTGRESQL,
    policy: AccessPolicy | None = None,
) -> None:
    """Validate SQL and raise SqlValidationError or AccessDeniedError."""
    active_policy = _resolve_policy(policy)
    if not sql or not sql.strip():
        raise SqlValidationError("SQL query cannot be empty")

    try:
        parsed = sqlglot.parse(sql, read=_dialect(db_type))
    except sqlglot.errors.ParseError as exc:
        raise SqlValidationError(f"SQL parse error: {exc}") from exc
    except Exception as exc:
        raise SqlValidationError(f"SQL validation error: {exc}") from exc

    statements = [item for item in parsed if item is not None]
    if len(statements) != 1:
        if len(statements) > 1:
            raise SqlValidationError(
                "Multiple statements are not allowed. Only a single SELECT query is permitted."
            )
        raise SqlValidationError("Failed to parse SQL query")

    statement = statements[0]
    if _is_explain(statement):
        _validate_explain(statement, db_type, active_policy)
        return

    if isinstance(statement, exp.Command):
        raise SqlValidationError("Only SELECT statements are allowed")

    _check_readonly_shape(statement)
    _check_functions(statement, active_policy)
    _check_tables(statement, active_policy)
    _check_columns(statement, active_policy)


def validate_sql(
    sql: str,
    db_type: DatabaseType = DatabaseType.POSTGRESQL,
    policy: AccessPolicy | None = None,
) -> tuple[bool, str | None]:
    """
    Validate SQL query using sqlglot.

    Args:
        sql: SQL query string to validate
        db_type: Database type (PostgreSQL or MySQL)
        policy: Optional access policy. Defaults to the configured global policy.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        validate_or_raise(sql, db_type, policy)
        return True, None
    except SqlValidationError as exc:
        return False, str(exc)


def add_limit_if_missing(
    sql: str, limit: int = 1000, db_type: DatabaseType = DatabaseType.POSTGRESQL
) -> str:
    """
    Add LIMIT clause to SELECT statement if missing.

    Args:
        sql: SQL query string
        limit: Maximum number of rows to return (default: 1000)
        db_type: Database type (PostgreSQL or MySQL)

    Returns:
        SQL query with LIMIT clause added if missing
    """
    try:
        dialect = _dialect(db_type)
        parsed = sqlglot.parse_one(sql, dialect=dialect)
        if parsed is None:
            return sql

        if parsed.find(exp.Limit):
            return sql

        parsed.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
        return parsed.sql(dialect=dialect)
    except Exception:
        return sql


def _is_explain_text(sql: str) -> bool:
    return sql.strip().upper().startswith("EXPLAIN")


def validate_and_transform_sql(
    sql: str,
    limit: int = 1000,
    db_type: DatabaseType = DatabaseType.POSTGRESQL,
    policy: AccessPolicy | None = None,
) -> str:
    """
    Validate SQL and add LIMIT if missing.

    EXPLAIN is returned unchanged after the policy accepts it, so the plan
    request is not rewritten into a different statement.

    Raises:
        SqlValidationError: If SQL validation fails
        AccessDeniedError: If the access policy rejects the SQL
    """
    validate_or_raise(sql, db_type, policy)
    if _is_explain_text(sql):
        return sql.strip().rstrip(";")
    return add_limit_if_missing(sql, limit, db_type)

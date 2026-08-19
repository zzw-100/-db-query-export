"""Query result export service.

Supports exporting query results to CSV and JSON formats.
"""

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.models.schemas import QueryResult


class ExportFormat(str, Enum):
    """Supported export formats."""

    CSV = "csv"
    JSON = "json"


def _json_default(value: Any) -> Any:
    """Serialize types that are not JSON-native (dates, decimals, bytes...)."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def to_csv(result: QueryResult) -> str:
    """
    Convert a query result to CSV text.

    Uses utf-8-sig (BOM) so that Chinese characters open correctly in Excel.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    # Header row from column names
    writer.writerow([column.name for column in result.columns])

    # Data rows, in column order
    for row in result.rows:
        writer.writerow([row.get(column.name, "") for column in result.columns])

    return buffer.getvalue()


def to_json(result: QueryResult) -> str:
    """Convert a query result to pretty-printed JSON text."""
    payload = {
        "sql": result.sql,
        "rowCount": result.row_count,
        "columns": [column.name for column in result.columns],
        "rows": result.rows,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def render(result: QueryResult, fmt: ExportFormat) -> tuple[bytes, str, str]:
    """
    Render a query result into the requested export format.

    Returns:
        (content_bytes, media_type, file_extension)
    """
    if fmt == ExportFormat.CSV:
        # utf-8-sig writes a BOM so Excel detects UTF-8 and shows Chinese correctly
        return to_csv(result).encode("utf-8-sig"), "text/csv; charset=utf-8", "csv"
    else:
        return to_json(result).encode("utf-8"), "application/json; charset=utf-8", "json"

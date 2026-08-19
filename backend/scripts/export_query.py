"""One-shot query + export automation tool.

Executes a SQL query against a registered database connection and exports
the result to a local file (CSV or JSON) in a single command, e.g.:

    uv run python scripts/export_query.py \
        --db interview_db \
        --sql "SELECT id, first_name, last_name FROM candidates LIMIT 10" \
        --format csv

This automates the three subtasks as one pipeline:
    1. fetch query result   (POST /api/v1/dbs/{name}/query/export executes the SQL)
    2. format data          (backend renders CSV/JSON)
    3. create file          (this script writes the downloaded bytes to disk)
"""

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_API_BASE = "http://localhost:8000"


def export_query(
    api_base: str,
    db_name: str,
    sql: str,
    fmt: str,
    output: str | None,
) -> Path:
    """Call the backend export endpoint and save the file locally."""
    url = f"{api_base.rstrip('/')}/api/v1/dbs/{db_name}/query/export"
    payload = json.dumps({"sql": sql, "format": fmt}).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        # Bypass any system proxy: the backend API is always reached directly
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=120) as response:
            content = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Export failed (HTTP {e.code}): {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Cannot reach backend at {api_base}: {e.reason}\n"
            "Please start the backend first: cd backend && uv run uvicorn app.main:app --port 8000"
        )

    # Decide output path
    if output:
        out_path = Path(output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path(f"{db_name}_export_{timestamp}.{fmt}")

    out_path.write_bytes(content)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot: execute a SQL query and export the result to CSV/JSON."
    )
    parser.add_argument("--db", required=True, help="Database connection name (e.g. interview_db)")
    parser.add_argument("--sql", required=True, help="SQL SELECT query to execute")
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Export format (default: csv)",
    )
    parser.add_argument("--out", default=None, help="Output file path (default: <db>_export_<timestamp>.<fmt>)")
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"Backend API base URL (default: {DEFAULT_API_BASE})",
    )
    args = parser.parse_args()

    print(f"[1/3] Executing query on '{args.db}' ...")
    print(f"      {args.sql}")
    out_path = export_query(args.api_base, args.db, args.sql, args.format, args.out)
    print(f"[2/3] Result formatted as {args.format.upper()}")
    print(f"[3/3] File created: {out_path.resolve()} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

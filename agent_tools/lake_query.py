"""Run DuckDB SQL over the Iceberg lake tables.

Each lake table the SQL names is read with pyiceberg into Arrow and registered as a view of the same name.
DuckDB's iceberg extension is not used, so nothing is downloaded. External access is switched off after the
views exist, so the SQL cannot read arbitrary files. Nothing here writes to the lake."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from typing import Any

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError

from agent_tools.lake_tables import NAMESPACE, TABLES

__all__ = ["query", "render_table", "rows_to_json", "tables_named"]


def tables_named(sql: str, names: Iterable[str]) -> tuple[str, ...]:
    """The names that appear in `sql` as whole words, ignoring case, in the order `names` gives them."""
    return tuple(n for n in names if re.search(rf"\b{re.escape(n)}\b", sql, re.IGNORECASE))


def _json_safe(value: Any) -> Any:
    """Timestamps become ISO text and any other non-JSON type becomes its string."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def rows_to_json(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    return [{c: _json_safe(v) for c, v in zip(columns, row, strict=True)} for row in rows]


def render_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Left-aligned columns under a dashed rule; None shows as NULL."""
    cells = [[("NULL" if v is None else str(v)) for v in row] for row in rows]
    widths = [max([len(c), *(len(r[i]) for r in cells)]) for i, c in enumerate(columns)]
    line = lambda parts: "  ".join(p.ljust(w) for p, w in zip(parts, widths, strict=True)).rstrip()  # noqa: E731
    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def _arrow_of(catalog: Catalog, name: str):
    """The table as Arrow, or None when the lake has no such table."""
    try:
        return catalog.load_table(f"{NAMESPACE}.{name}").scan().to_arrow()
    except NoSuchTableError:
        return None


def query(catalog: Catalog, sql: str) -> tuple[list[str], list[tuple]]:
    """Edge. Register the lake tables `sql` names, lock the connection to memory, run it and return (columns, rows).

    A table the lake lacks is left unregistered, so DuckDB reports it missing in its own words."""
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        for name in tables_named(sql, TABLES):
            arrow = _arrow_of(catalog, name)
            if arrow is not None:
                con.register(name, arrow)
        con.execute("SET TimeZone = 'UTC'")
        con.execute("SET enable_external_access = false")
        # Not fetchall(): DuckDB's fetchall on a timestamptz column imports pytz, which no extra declares.
        cursor = con.execute(sql)
        # to_arrow_table replaces fetch_arrow_table in newer duckdb; the lake extra allows any duckdb>=1.0.
        result = (getattr(cursor, "to_arrow_table", None) or cursor.fetch_arrow_table)()
        return result.column_names, list(zip(*(c.to_pylist() for c in result.columns), strict=True))
    finally:
        con.close()

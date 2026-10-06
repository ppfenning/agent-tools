"""Append new store rows to the Iceberg tables behind a per-table high-water mark.

A run syncs once, when it has ended: appends are append-only, so a live run's rows must never reach the lake.
`upper` is the largest non-null `runs.ended_at`, read once. A table's mark is the `runs.ended_at` it has synced
through, kept in the HWM_PROPERTY of the Iceberg table. Rows for runs with `mark < ended_at <= upper` are appended
and the mark moves to `upper` in the same transaction, so a crash cannot advance a mark without its rows.
Ledger rows whose run_id has no run row are skipped: the ledger predates the store.
Store timestamps are ISO UTC text with one format, so text order is time order."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pyiceberg.catalog import Catalog
from pyiceberg.types import BooleanType, StringType, TimestamptzType

from agent_tools.lake_tables import HWM_PROPERTY, NAMESPACE, TABLES, TableDef
from agent_tools.store_dialect import connect_readonly_url, placeholder

if TYPE_CHECKING:
    import pyarrow as pa

# A missing pyarrow must still fail this import: `cox lake sync` imports the module outside its lease and refuses
# with exit 2, while an ImportError raised later inside the lease becomes a quiet "sync skipped". find_spec loads nothing.
if importlib.util.find_spec("pyarrow") is None:
    raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")

__all__ = ["SyncResult", "next_hwm", "rows_after", "sync", "to_arrow"]

HISTORY = ("runs", "phases", "node_calls", "gate_decisions", "ledger")
_ENDED = "_ended_at"

Row = Mapping[str, Any]


@dataclass(frozen=True)
class SyncResult:
    table: str
    rows_found: int
    rows_appended: int
    old_mark: str | None
    new_mark: str | None


def rows_after(rows: Sequence[Row], column: str, hwm: str | None) -> list[Row]:
    """Rows whose `column` is strictly greater than `hwm`; a None hwm keeps every row."""
    return [r for r in rows if hwm is None or (r[column] is not None and r[column] > hwm)]


def next_hwm(rows: Sequence[Row], column: str, hwm: str | None) -> str | None:
    """The largest `column` value in `rows`, or the old `hwm` when none is larger."""
    return max((v for v in [hwm, *(r[column] for r in rows)] if v is not None), default=None)


def _cell(kind: object, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(kind, TimestamptzType):
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    if isinstance(kind, BooleanType):
        return bool(value)
    if isinstance(kind, StringType) and isinstance(value, dict | list):
        return json.dumps(value)
    return value


def to_arrow(table_def: TableDef, rows: Sequence[Row]) -> pa.Table:
    """An Arrow table in the lake schema: ISO text becomes a UTC timestamp, 0/1 becomes a bool."""
    import pyarrow as pa

    arrow_schema = table_def.schema.as_arrow()
    arrays = [
        pa.array([_cell(field.field_type, r[field.name]) for r in rows], type=arrow_schema.field(field.name).type)
        for field in table_def.schema.fields
    ]
    return pa.Table.from_arrays(arrays, schema=arrow_schema)


def _query(name: str, table_def: TableDef, token: str, has_mark: bool) -> str:
    columns = ", ".join(f"t.{f.name}" for f in table_def.schema.fields)
    lower = f" AND r.ended_at > {token}" if has_mark else ""
    return (
        f"SELECT {columns}, r.ended_at AS {_ENDED} FROM {name} t JOIN runs r ON r.run_id = t.run_id "
        f"WHERE r.ended_at <= {token}{lower}"
    )


def _sync_table(catalog: Catalog, conn, token: str, name: str, upper: str, dry_run: bool) -> SyncResult:
    table = catalog.load_table(f"{NAMESPACE}.{name}")
    old = table.properties.get(HWM_PROPERTY)
    args = (upper, old) if old is not None else (upper,)
    cursor = conn.execute(_query(name, TABLES[name], token, old is not None), args)
    rows = rows_after([dict(r) for r in cursor.fetchall()], _ENDED, old)
    new = next_hwm([{_ENDED: upper}], _ENDED, old)
    if dry_run:
        return SyncResult(name, len(rows), 0, old, new)
    if rows or new != old:
        # A window with no rows still moves the mark, in a commit that writes no snapshot.
        with table.transaction() as tx:
            if rows:
                tx.append(to_arrow(TABLES[name], rows))
            tx.set_properties({HWM_PROPERTY: new})
    return SyncResult(name, len(rows), len(rows), old, new)


def sync(catalog: Catalog, store_url: str, dry_run: bool = False) -> list[SyncResult]:
    """Edge. One result per history table. With `dry_run` nothing is written and `rows_appended` is 0.

    `new_mark` is the mark the table holds afterwards, or would hold on a dry run."""
    token = placeholder(store_url)
    with closing(connect_readonly_url(store_url)) as conn:
        upper = conn.execute("SELECT MAX(ended_at) AS upper FROM runs").fetchone()["upper"]
        if upper is None:
            return [SyncResult(name, 0, 0, None, None) for name in HISTORY]
        return [_sync_table(catalog, conn, token, name, upper, dry_run) for name in HISTORY]

"""A local Parquet snapshot of four store tables, refreshed by age, queried with DuckDB.

duckdb is imported here and nowhere else, and only inside the edge functions, so a machine
without the optional extra `analytics` still imports this module. `query` then hands back the
caller's `fallback()`, which runs the existing Postgres query: this module never imports a
store driver.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from functools import reduce
from pathlib import Path
from urllib.parse import unquote, urlsplit

from agent_tools.lake_config import redact

DEFAULT_MAX_AGE_S = 300
TABLES = ("node_calls", "chair_actions", "runs", "task_records")
META_FILE = "built_at.json"
GENERATION_PREFIX = "analytics.gen-"

_LOG = logging.getLogger(__name__)
# Edge state: the once-per-process flag for the fallback warning. Nothing in the pure core reads it.
_warned = False


def snapshot_is_fresh(built_at: float | None, now: float, max_age_s: int) -> bool:
    """None means no snapshot. An age equal to `max_age_s` is stale."""
    return built_at is not None and now - built_at < max_age_s


def max_age_s(profile: Mapping) -> int:
    """`analytics.snapshot_max_age_s` from the profile; a missing, non-numeric or negative value gives 300."""
    section = profile.get("analytics")
    value = section.get("snapshot_max_age_s") if isinstance(section, Mapping) else None
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return int(value) if ok else DEFAULT_MAX_AGE_S


def snapshot_paths(runs_dir: Path | str) -> dict[str, Path]:
    """One Parquet file per table and the `meta` file, all under `<runs_dir>/analytics/`."""
    base = Path(runs_dir) / "analytics"
    return {**{table: base / f"{table}.parquet" for table in TABLES}, "meta": base / META_FILE}


def _scrub(text: str, store_url: str) -> str:
    """`text` with the store URL, any URL password and the raw password removed.

    `redact` only matches a URL at the start of its input, so it is applied to each token."""
    swapped = text.replace(store_url, redact(store_url)) if store_url else text
    tokens = re.sub(r"[^\s'\"]+", lambda m: redact(m.group(0)), swapped)
    password = urlsplit(store_url).password or ""
    secrets = sorted({password, unquote(password)} - {""}, key=len, reverse=True)
    return reduce(lambda acc, secret: acc.replace(secret, "***"), secrets, tokens)


def _safe_message(exc: BaseException, store_url: str) -> str:
    return f"{type(exc).__name__}: {_scrub(str(exc), store_url)}"


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _read_built_at(meta: Path) -> float | None:
    """Edge. When the snapshot was built; None when the file is absent or unreadable."""
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))["built_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _duckdb_copy(store_url: str, dest: Path) -> None:
    """Edge. Copy the four tables from Postgres into `dest` as Parquet with DuckDB's postgres extension."""
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL postgres")
        con.execute("LOAD postgres")
        con.execute(f"ATTACH {_quote(store_url)} AS pg (TYPE postgres, READ_ONLY)")
        # The store creates these tables unqualified (stats_schema.py), so they sit in the connection's default
        # schema. `public` is that schema unless the URL sets a search_path; a store that does needs its own copy.
        for table in TABLES:
            con.execute(f"COPY (SELECT * FROM pg.public.{table}) TO {_quote(str(dest / f'{table}.parquet'))} (FORMAT PARQUET)")
    finally:
        con.close()


def refresh_snapshot(
    runs_dir: Path | str,
    store_url: str,
    now: float,
    max_age_s: int,
    copy: Callable[[str, Path], None] = _duckdb_copy,
) -> None:
    """Edge. Do nothing when the snapshot is fresh; else copy into a temp directory and rename it into place."""
    root = Path(runs_dir)
    if snapshot_is_fresh(_read_built_at(snapshot_paths(root)["meta"]), now, max_age_s):
        return
    # `analytics` is a symlink to a generation directory. Renaming one directory over another is two steps with a
    # gap where `analytics/` is absent, and a reader in that gap would start a second copy. Replacing a symlink is
    # one atomic `os.replace`, so a reader sees the whole old snapshot or the whole new one.
    final = root / "analytics"
    root.mkdir(parents=True, exist_ok=True)
    gen = Path(tempfile.mkdtemp(prefix=GENERATION_PREFIX, dir=root))
    link = gen.with_name(gen.name + ".link")
    legacy = root / f"{GENERATION_PREFIX}legacy-{gen.name.removeprefix(GENERATION_PREFIX)}"
    try:
        copy(store_url, gen)
        (gen / META_FILE).write_text(json.dumps({"built_at": now}), encoding="utf-8")
        link.symlink_to(gen.name)
        if final.exists() and not final.is_symlink():
            final.rename(legacy)  # a real `analytics/` directory is migrated once; this is the only gap
        previous = legacy.name if legacy.exists() else (Path(os.readlink(final)).name if final.is_symlink() else None)
        os.replace(link, final)
    except Exception:
        if legacy.exists() and not final.exists():
            legacy.rename(final)
        link.unlink(missing_ok=True)
        shutil.rmtree(gen, ignore_errors=True)
        raise
    # Remove only the generation this refresh replaced. A glob over every `analytics.gen-*` would also delete
    # another process's in-flight copy, or the one it has just made current, and leave `analytics` dangling.
    if previous is not None:
        shutil.rmtree(root / previous, ignore_errors=True)


def _snapshot_connection(duckdb, runs_dir: Path | str):
    """Edge. An in-memory connection with one view per snapshot table. An unreadable snapshot raises here."""
    paths = snapshot_paths(runs_dir)
    con = duckdb.connect()
    try:
        for table in TABLES:
            con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet({_quote(str(paths[table]))})")
    except Exception:
        con.close()
        raise
    return con


def _warn_once(message: str) -> None:
    """Edge. Log `message` the first time in this process and never again."""
    global _warned  # noqa: PLW0603
    if not _warned:
        _warned = True
        _LOG.warning("analytics snapshot unavailable, using the Postgres fallback: %s", message)


def query(
    sql: str,
    params: Sequence | None,
    *,
    runs_dir: Path | str,
    store_url: str,
    now: float,
    max_age_s: int,
    fallback: Callable[[], list[tuple]],
) -> list[tuple]:
    """Edge. Rows of `sql` over the snapshot, the four tables available by name.

    A missing duckdb or a failed refresh logs one warning per process and returns `fallback()`."""
    try:
        import duckdb

        refresh_snapshot(runs_dir, store_url, now, max_age_s)
        con = _snapshot_connection(duckdb, runs_dir)
    except Exception as exc:
        _warn_once(_safe_message(exc, store_url))
        return fallback()
    try:
        return [tuple(row) for row in con.execute(sql, list(params or ())).fetchall()]
    finally:
        con.close()

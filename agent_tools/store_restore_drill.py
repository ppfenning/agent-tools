"""Monthly restore drill: restore the newest Garage dump into a scratch database and compare it with the live store."""

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import chair_read_record, store_backup, store_cli, store_url
from agent_tools.store_backup import PREFIX, GarageClient, newest_key
from agent_tools.store_drill_compare import TABLES, CompareResult, Snapshot, compare

HOLDER = "store-restore-drill"
TOLERANCE = 0.1  # the dump is up to a day old and live keeps growing, so restored may trail live by this fraction
# unknown: the live store's schema table is not named in this repo; this is the query to adjust if it is not this one.
VERSION_SQL = "SELECT max(version) FROM schema_version"
REASON_LIMIT = 600  # chair_types.Action: a needs_chair reason is trimmed to this many characters

Run = Callable[..., Any]
Query = Callable[[str], str]


class DrillFailed(RuntimeError):
    def __init__(self, cause: str, reason: str) -> None:
        super().__init__(reason)
        self.cause, self.reason = cause, reason


class QueryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    container: str
    user: str
    database: str


def scratch_name(token: str) -> str:
    """A scratch database name for `token`, which must be alphanumeric so the name is a safe identifier."""
    if not token.isalnum():
        raise ValueError("a scratch database token must be alphanumeric")
    return f"drill_{token.lower()}"


def _docker(target: Target, *argv: str, interactive: bool = False) -> list[str]:
    return ["docker", "exec", *(["-i"] if interactive else []), target.container, *argv]


def create_argv(target: Target, scratch: str) -> list[str]:
    return _docker(target, "createdb", "-U", target.user, scratch)


def restore_argv(target: Target, scratch: str) -> list[str]:
    return _docker(target, "pg_restore", "-U", target.user, "-d", scratch, "--no-owner", interactive=True)


def drop_argv(target: Target, scratch: str) -> list[str]:
    return _docker(target, "dropdb", "-U", target.user, "--if-exists", scratch)


def query_argv(target: Target, database: str, sql: str) -> list[str]:
    return _docker(target, "psql", "-U", target.user, "-d", database, "-At", "-c", sql)


def read_snapshot(query: Query) -> Snapshot:
    """A table that cannot be counted is left out of the snapshot, which `compare` reports as a missing table."""
    counts: dict[str, int] = {}
    for table in TABLES:
        try:
            counts[table] = int(query(f"SELECT count(*) FROM {table}").strip())
        except QueryError, ValueError:
            continue
    return Snapshot(query(VERSION_SQL).strip(), counts)


def failure_findings(result: CompareResult) -> str:
    return "; ".join(f"{f.check}: {f.detail}" for f in result.findings if not f.ok)


def drill_action(key: str) -> dict[str, Any]:
    # unknown: no ActionKind names a drill, and chair_types.py is out of this item's scope; the log takes any kind.
    return {"kind": "restore_drill", "status": "recorded", "key": key, "reason": f"restored {key}; comparison passed"}


def failure_action(cause: str, reason: str) -> dict[str, Any]:
    """A needs_chair row; `initiative` is what gives the inbox a stable target for it."""
    return {
        "kind": "needs_chair",
        "status": "recorded",
        "initiative": HOLDER,
        "cause": cause,
        "reason": reason[:REASON_LIMIT],
    }


def _stderr(done: Any) -> str:
    text = done.stderr
    return (text.decode("utf-8", "replace") if isinstance(text, bytes) else text or "").strip()


def _ok(done: Any, cause: str, what: str) -> None:
    if done.returncode != 0:
        raise DrillFailed(cause, f"{what} exited {done.returncode}: {_stderr(done)}")


def _drop(run: Run, target: Target, scratch: str) -> None:
    try:
        done = run(drop_argv(target, scratch), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as err:
        print(f"restore drill: scratch database {scratch} not dropped: {err}", file=sys.stderr)
        return
    if done.returncode != 0:
        print(f"restore drill: scratch database {scratch} not dropped: {_stderr(done)}", file=sys.stderr)


def _restore_and_compare(
    client: GarageClient, run: Run, target: Target, scratch: str, query_for: Callable[[str], Query], tolerance: float
) -> tuple[str, CompareResult]:
    """Edge. Download, restore into `scratch`, read both sides and compare; the scratch database is dropped on every path."""
    key = newest_key(client.list(PREFIX))
    if key is None:
        raise DrillFailed("restore_drill_no_dump", f"no dump under {PREFIX} in Garage")
    with tempfile.TemporaryDirectory(prefix="store-restore-drill-") as workdir:
        dump = Path(workdir) / key.removeprefix(PREFIX)
        client.get(key, dump)
        try:
            _ok(
                run(create_argv(target, scratch), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False),
                "restore_drill_restore_failed",
                "createdb",
            )
            with dump.open("rb") as src:
                done = run(
                    restore_argv(target, scratch),
                    stdin=src,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            _ok(done, "restore_drill_restore_failed", "pg_restore")
            result = compare(read_snapshot(query_for(scratch)), read_snapshot(query_for(target.database)), tolerance)
        finally:
            _drop(run, target, scratch)
    return key, result


def run_drill(
    client: GarageClient,
    run: Run,
    target: Target,
    scratch: str,
    query_for: Callable[[str], Query],
    record: Callable[[Mapping[str, Any]], None],
    tolerance: float = TOLERANCE,
) -> str:
    """Edge. Record one chair action for the drill and return the dump key; a failed drill records needs_chair and raises."""
    try:
        key, result = _restore_and_compare(client, run, target, scratch, query_for, tolerance)
        if not result.passed:
            raise DrillFailed("restore_drill_compare_failed", failure_findings(result))
    except DrillFailed as err:
        record(failure_action(err.cause, err.reason))
        raise
    except Exception as err:
        record(failure_action("restore_drill_error", str(err)))
        raise DrillFailed("restore_drill_error", str(err)) from err
    record(drill_action(key))
    return key


def psql_query(run: Run, target: Target) -> Callable[[str], Query]:
    """Edge. `query_for(database)` gives a function that runs one SQL statement there through `run` and returns its output."""

    def query_for(database: str) -> Query:
        def query(sql: str) -> str:
            done = run(query_argv(target, database, sql), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if done.returncode != 0:
                raise QueryError(f"psql exited {done.returncode}: {_stderr(done)}")
            out = done.stdout
            return out.decode("utf-8", "replace") if isinstance(out, bytes) else out or ""

        return query

    return query_for


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] = os.environ,
    client: GarageClient | None = None,
    runner: Run = subprocess.run,
    record: Callable[[Mapping[str, Any]], None] | None = None,
    now: datetime | None = None,
    token: str | None = None,
) -> int:
    """Edge. One drill; 0 only when the restored dump matches the live store, else 1 with the reason on stderr."""
    parser = argparse.ArgumentParser(prog="python -m agent_tools.store_restore_drill", description=__doc__)
    parser.add_argument("--container", required=True, help="name of the store's Postgres container")
    parser.add_argument("--runs-dir", default="runs", help="the chair's runs directory, where the action is recorded")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE, help="fraction restored rows may trail live")
    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir)
    try:
        profile = store_backup.load_profile(env)
        pg_user, pg_db = store_backup.dump_target(store_url.profile_store_url(profile, runs_dir))
        garage = (
            client
            if client is not None
            else store_backup.ArrowGarage.connect(store_backup.garage_settings(profile, env))
        )
        target = Target(args.container, pg_user, pg_db)
        stamp = now or datetime.now(UTC)
        write = record or chair_read_record.recorder(
            runs_dir,
            lambda: -1,  # no chair lease applies to a drill, so the row carries the epoch no lease matches
            lambda: stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            store=store_cli.runner(runs_dir),
            holder=HOLDER,
        )
        key = run_drill(
            garage,
            runner,
            target,
            scratch_name(token or secrets.token_hex(4)),
            psql_query(runner, target),
            write,
            args.tolerance,
        )
    except Exception as err:
        print(f"store restore drill failed: {err}", file=sys.stderr)
        return 1
    print(f"store restore drill ok: {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

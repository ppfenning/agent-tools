import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools.store_backup import backup_key
from agent_tools.store_restore_drill import (
    VERSION_SQL,
    DrillFailed,
    QueryError,
    Target,
    drill_action,
    failure_action,
    main,
    run_drill,
    scratch_name,
)

NOW = datetime(2026, 10, 7, 3, 15, 0, tzinfo=UTC)
KEY = backup_key(NOW)
OLD_KEY = backup_key(datetime(2026, 10, 6, 3, 15, 0, tzinfo=UTC))
TARGET = Target("store-db", "cox", "coxstore")
SCRATCH = "drill_ab12"
LIVE = {"runs": 100, "work_items": 50, "chair_actions": 200}


class FakeGarage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})

    def put(self, key: str, path: Path) -> None:
        self.objects[key] = path.read_bytes()

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str, path: Path) -> None:
        path.write_bytes(self.objects[key])

    def delete(self, key: str) -> None:
        del self.objects[key]

    def size(self, key: str) -> int | None:
        return len(self.objects[key]) if key in self.objects else None


class FakeRunner:
    """Logs every argv; a command whose tool name is in `fail` exits 1."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []
        self.restored: list[bytes] = []

    def __call__(self, argv, *, stdout, stderr, check, stdin=None):
        self.calls.append(list(argv))
        if stdin is not None:
            self.restored.append(stdin.read())
        tool = next(a for a in argv if a in ("createdb", "pg_restore", "dropdb", "psql"))
        failed = tool in self.fail
        return SimpleNamespace(returncode=1 if failed else 0, stdout=b"", stderr=b"boom" if failed else b"")

    def tools(self) -> list[str]:
        return [next(a for a in c if a in ("createdb", "pg_restore", "dropdb", "psql")) for c in self.calls]


def fake_query_for(restored: dict[str, int], restored_version="12", live_version="12"):
    def query_for(database: str):
        counts, version = (restored, restored_version) if database == SCRATCH else (LIVE, live_version)

        def query(sql: str) -> str:
            if sql == VERSION_SQL:
                return f"{version}\n"
            table = sql.removeprefix("SELECT count(*) FROM ")
            if table not in counts:
                raise QueryError("no such table")
            return f"{counts[table]}\n"

        return query

    return query_for


@pytest.fixture
def workroot(tmp_path, monkeypatch):
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def drill(garage, runner, query_for):
    records: list[dict] = []
    return records, lambda: run_drill(garage, runner, TARGET, SCRATCH, query_for, records.append, 0.1)


def test_scratch_name_is_a_safe_identifier():
    assert scratch_name("AB12") == "drill_ab12"
    with pytest.raises(ValueError):
        scratch_name("x; drop")


def test_failure_action_is_a_needs_chair_row_with_a_target():
    action = failure_action("restore_drill_no_dump", "x" * 700)
    assert (action["kind"], action["initiative"], len(action["reason"])) == ("needs_chair", "store-restore-drill", 600)


def test_passing_drill_records_one_action_and_drops_the_scratch_database(workroot):
    garage, runner = FakeGarage({OLD_KEY: b"old", KEY: b"PGDMP-new"}), FakeRunner()
    records, go = drill(garage, runner, fake_query_for(LIVE))
    assert go() == KEY
    assert records == [drill_action(KEY)]
    assert runner.tools() == ["createdb", "pg_restore", "dropdb"]
    assert runner.calls[-1][-1] == SCRATCH
    assert runner.restored == [b"PGDMP-new"]
    assert list(workroot.iterdir()) == []


def test_failing_comparison_raises_needs_chair_and_still_drops(workroot):
    runner = FakeRunner()
    records, go = drill(FakeGarage({KEY: b"PGDMP"}), runner, fake_query_for({**LIVE, "runs": 10}))
    with pytest.raises(DrillFailed, match="runs: restored=10 live=100"):
        go()
    assert [(r["kind"], r["cause"]) for r in records] == [("needs_chair", "restore_drill_compare_failed")]
    assert runner.tools()[-1] == "dropdb"
    assert list(workroot.iterdir()) == []


def test_schema_mismatch_fails_the_drill(workroot):
    records, go = drill(FakeGarage({KEY: b"PGDMP"}), FakeRunner(), fake_query_for(LIVE, restored_version="11"))
    with pytest.raises(DrillFailed, match="schema_version"):
        go()
    assert records[0]["kind"] == "needs_chair"


def test_empty_bucket_raises_needs_chair_and_restores_nothing(workroot):
    runner = FakeRunner()
    records, go = drill(FakeGarage({"store-backup/notes.txt": b"n"}), runner, fake_query_for(LIVE))
    with pytest.raises(DrillFailed, match="no dump"):
        go()
    assert [(r["kind"], r["cause"]) for r in records] == [("needs_chair", "restore_drill_no_dump")]
    assert runner.calls == []


def test_failed_restore_raises_needs_chair_drops_and_cleans_up(workroot):
    runner = FakeRunner(fail=("pg_restore",))
    records, go = drill(FakeGarage({KEY: b"PGDMP"}), runner, fake_query_for(LIVE))
    with pytest.raises(DrillFailed, match="pg_restore exited 1: boom"):
        go()
    assert [r["cause"] for r in records] == ["restore_drill_restore_failed"]
    assert runner.tools() == ["createdb", "pg_restore", "dropdb"]
    assert list(workroot.iterdir()) == []


def test_failed_drop_does_not_mask_a_passing_drill(workroot, capsys):
    records, go = drill(FakeGarage({KEY: b"PGDMP"}), FakeRunner(fail=("dropdb",)), fake_query_for(LIVE))
    assert go() == KEY
    assert len(records) == 1
    assert f"scratch database {SCRATCH} not dropped: boom" in capsys.readouterr().err


PROVIDER_YAML = """\
lake_url: s3://lake-bucket/warehouse
storage_url: postgresql://cox:pw@store-db:5432/coxstore
object_store:
  endpoint: http://garage.example:3900
  access_key_env: G_KEY
  secret_key_env: G_SECRET
"""


def profile_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "provider.yaml").write_text(PROVIDER_YAML, encoding="utf-8")
    (tmp_path / "profile.yaml").write_text(f"provider_profile: {tmp_path / 'provider.yaml'}\n", encoding="utf-8")
    return {"AGENT_TOOLS_PROFILE": str(tmp_path / "profile.yaml"), "G_KEY": "k", "G_SECRET": "s"}


class CountingRunner(FakeRunner):
    """Answers every psql count with the live figure and the schema query with 12."""

    def __call__(self, argv, **kw):
        done = super().__call__(argv, **kw)
        if "psql" in argv:
            sql = argv[-1]
            out = "12\n" if sql == VERSION_SQL else f"{LIVE[sql.rsplit(' ', 1)[-1]]}\n"
            return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        return done


def test_main_success_and_failure_exit_codes(workroot, tmp_path, capsys):
    env, records = profile_env(tmp_path), []
    runner = CountingRunner()
    code = main(
        ["--container", "store-db"],
        env=env,
        client=FakeGarage({KEY: b"PGDMP"}),
        runner=runner,
        record=records.append,
        now=NOW,
        token="ab12",
    )
    assert (code, capsys.readouterr().out) == (0, f"store restore drill ok: {KEY}\n")
    assert [r["kind"] for r in records] == ["restore_drill"]
    assert runner.calls[0] == ["docker", "exec", "store-db", "createdb", "-U", "cox", "drill_ab12"]

    records.clear()
    code = main(
        ["--container", "store-db"], env=env, client=FakeGarage(), runner=runner, record=records.append, now=NOW
    )
    assert code == 1
    assert "store restore drill failed: no dump" in capsys.readouterr().err
    assert [r["kind"] for r in records] == ["needs_chair"]

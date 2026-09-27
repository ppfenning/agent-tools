import importlib.util
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import agent_tools
from agent_tools import cli

has_lake = importlib.util.find_spec("pyiceberg") is not None and importlib.util.find_spec("pyiceberg_core") is not None
needs_lake = pytest.mark.skipif(not has_lake, reason="the optional extra `lake` is not installed")

E2, E3 = "2026-09-25T06:00:00+00:00", "2026-09-25T07:00:00+00:00"


def _seed_store(path) -> None:
    from test_lake_sync import DDL, ROWS, _insert

    conn = sqlite3.connect(path)
    for ddl in DDL:
        conn.execute(ddl)
    for table, values in ROWS:
        _insert(conn, table, values)
    conn.commit()
    conn.close()


def _trace(path, run_id="r1") -> None:
    from test_lake_traces import _write

    _write(path, run_id)


@pytest.fixture
def env(tmp_path):
    """A SQLite store, catalog and warehouse under tmp_path, named by a routing and a provider profile."""
    store = tmp_path / "store.db"
    _seed_store(store)
    provider = tmp_path / "provider.yaml"
    provider.write_text(
        f"storage_url: sqlite:///{store}\nlake_catalog_url: sqlite:///{tmp_path}/lake-catalog.db\nlake_url: file://{tmp_path}/lake\n"
    )
    routing = tmp_path / "profile.yaml"
    routing.write_text(f"provider_profile: {provider}\n")
    return tmp_path, ["lake", "sync", "--profile", str(routing), "--runs-dir", str(tmp_path / "runs")]


def _run(argv, capsys):
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def _catalog(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    # The name must match lake_config.load_catalog's: a SqlCatalog only sees its own name's tables.
    return SqlCatalog("coxswain", uri=f"sqlite:///{tmp_path}/lake-catalog.db", warehouse=f"file://{tmp_path}/lake")


def _state(tmp_path) -> dict:
    """Per lake table: its mark and its current snapshot id."""
    from agent_tools.lake_tables import HWM_PROPERTY, NAMESPACE, TABLES

    catalog = _catalog(tmp_path)
    tables = {name: catalog.load_table(f"{NAMESPACE}.{name}") for name in TABLES}
    return {
        name: (t.properties.get(HWM_PROPERTY), t.current_snapshot() and t.current_snapshot().snapshot_id)
        for name, t in tables.items()
    }


@needs_lake
def test_the_first_run_appends_rows_and_the_second_appends_none(env, capsys) -> None:
    _, argv = env
    rc, first = _run(argv, capsys)
    assert rc == 0
    assert f"runs: 2 appended (mark {E2})" in first
    assert "phases: 3 appended" in first
    assert "traces: 0 registered, 0 skipped" in first
    rc, second = _run(argv, capsys)
    assert rc == 0
    assert f"runs: 0 appended (mark {E2})" in second
    assert all(" 0 appended" in line for line in second.splitlines() if not line.startswith("traces"))


@needs_lake
def test_dry_run_on_a_fresh_lake_reports_the_pending_rows_and_creates_nothing(env, capsys) -> None:
    tmp_path, argv = env
    rc, out = _run([*argv, "--dry-run"], capsys)
    assert rc == 0
    assert out.splitlines()[0].startswith("lake: would create coxswain.runs, coxswain.phases")
    assert f"runs: would append 2 (mark {E2})" in out
    assert f"phases: would append 3 (mark {E2})" in out
    assert "traces: would register 0, 0 skipped" in out
    assert not (tmp_path / "lake").exists()
    assert not (tmp_path / "lake-catalog.db").exists()


@needs_lake
def test_dry_run_on_a_catalog_file_with_no_catalog_tables_creates_none(env, capsys) -> None:
    # The same path as a fresh Postgres database: the catalog exists, so it is opened, and opening must not initialise it.
    tmp_path, argv = env
    sqlite3.connect(tmp_path / "lake-catalog.db").close()
    rc, out = _run([*argv, "--dry-run"], capsys)
    assert rc == 0
    assert out.splitlines()[0].startswith("lake: would create coxswain.runs")
    assert f"runs: would append 2 (mark {E2})" in out
    with sqlite3.connect(tmp_path / "lake-catalog.db") as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    assert not (tmp_path / "lake").exists()


@needs_lake
def test_dry_run_with_pending_rows_writes_nothing_and_the_real_run_then_appends_them(env, capsys) -> None:
    tmp_path, argv = env
    _run(argv, capsys)
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.execute("INSERT INTO runs VALUES ('r3', '2026-09-25T06:30:00+00:00', ?)", (E3,))
    conn.execute("INSERT INTO phases VALUES ('r3', 'scope', ?, '{}')", (E3,))
    conn.commit()
    conn.close()
    before = _state(tmp_path)
    rc, out = _run([*argv, "--dry-run"], capsys)
    assert rc == 0
    assert f"runs: would append 1 (mark {E3})" in out
    assert f"phases: would append 1 (mark {E3})" in out
    assert "would create" not in out
    assert _state(tmp_path) == before
    rc, out = _run(argv, capsys)
    assert rc == 0
    assert f"runs: 1 appended (mark {E3})" in out
    assert _state(tmp_path)["runs"] != before["runs"]


@needs_lake
def test_dry_run_on_a_partly_created_lake_reads_the_tables_it_has_and_names_the_one_it_lacks(env, capsys) -> None:
    tmp_path, argv = env
    _run(argv, capsys)
    _catalog(tmp_path).drop_table("coxswain.traces")
    rc, out = _run([*argv, "--dry-run", "--json"], capsys)
    report = json.loads(out)
    assert rc == 0
    assert report["new_tables"] == ["coxswain.traces"]
    assert {t["table"]: t["rows_found"] for t in report["tables"]}["runs"] == 0
    assert report["traces"] == {"found": 0, "registered": 0, "skipped": 0, "would_register": 0}
    assert not _catalog(tmp_path).table_exists("coxswain.traces")


@needs_lake
def test_traces_are_registered_once_and_a_dry_run_counts_the_files_it_would_register(env, capsys) -> None:
    tmp_path, argv = env
    traces = tmp_path / "runs" / "traces"
    _trace(traces / "2026/09/25/r1.parquet")
    rc, out = _run([*argv, "--dry-run"], capsys)
    assert rc == 0
    assert "lake: would create" in out
    assert "traces: would register 1, 0 skipped" in out
    assert not (tmp_path / "lake").exists()
    rc, out = _run(argv, capsys)
    assert "traces: 1 registered, 0 skipped" in out
    rc, out = _run(argv, capsys)
    assert "traces: 0 registered, 1 skipped" in out
    _trace(traces / "2026/09/26/r2.parquet", "r2")
    before = _state(tmp_path)
    rc, out = _run([*argv, "--dry-run"], capsys)
    assert "traces: would register 1, 1 skipped" in out
    assert _state(tmp_path) == before
    rc, out = _run(argv, capsys)
    assert "traces: 1 registered, 1 skipped" in out


@needs_lake
def test_the_provider_profiles_traces_url_replaces_the_runs_dir_default(env, capsys) -> None:
    tmp_path, argv = env
    with (tmp_path / "provider.yaml").open("a") as f:
        f.write(f"traces_url: {tmp_path}/elsewhere\n")
    _trace(tmp_path / "elsewhere" / "2026/09/25/r1.parquet")
    _trace(tmp_path / "runs" / "traces" / "2026/09/25/ignored.parquet", "ignored")
    rc, out = _run(argv, capsys)
    assert rc == 0
    assert "traces: 1 registered, 0 skipped" in out


@needs_lake
def test_the_default_relative_runs_dir_gives_an_absolute_lake_and_stable_trace_paths(tmp_path, monkeypatch, capsys) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _seed_store(tmp_path / "store.db")
    (tmp_path / "provider.yaml").write_text(f"storage_url: sqlite:///{tmp_path}/store.db\n")
    (tmp_path / "profile.yaml").write_text(f"provider_profile: {tmp_path}/provider.yaml\n")
    argv = ["lake", "sync", "--profile", str(tmp_path / "profile.yaml"), "--json"]
    _trace(work / "runs" / "traces" / "2026/09/25/r1.parquet")
    monkeypatch.chdir(work)
    rc, out = _run(argv, capsys)
    report = json.loads(out)
    assert rc == 0
    assert report["warehouse"] == f"file://{work}/runs/lake"
    assert report["traces_root"] == f"{work}/runs/traces"
    assert report["traces"]["registered"] == 1
    assert (work / "runs" / "lake-catalog.db").exists()
    monkeypatch.chdir(tmp_path)
    rc, out = _run([*argv, "--runs-dir", str(work / "runs")], capsys)
    assert json.loads(out)["traces"] == {"found": 1, "registered": 0, "skipped": 1, "would_register": 0}


@needs_lake
def test_the_lake_modules_read_only_what_the_dry_run_preview_supplies(env, capsys) -> None:
    """The dry run hands the lake modules a stand-in catalog and stand-in tables; this fails if they start to want more."""
    from agent_tools import lake_sync, lake_traces

    tmp_path, argv = env
    _run(argv, capsys)
    seen: dict[str, set[str]] = {}

    class Spy:
        def __init__(self, target, key):
            self._target, self._key = target, key

        def __getattr__(self, name):
            seen.setdefault(self._key, set()).add(name)
            return getattr(self._target, name)

        def load_table(self, identifier):
            seen.setdefault(self._key, set()).add("load_table")
            return Spy(self._target.load_table(identifier), identifier)

    spy = Spy(_catalog(tmp_path), "catalog")
    lake_sync.sync(spy, f"sqlite:///{tmp_path}/store.db", dry_run=True)
    lake_traces.register_traces(spy, str(tmp_path / "runs" / "traces"), dry_run=True)
    assert seen.pop("catalog") == {"load_table"}
    assert seen.pop("coxswain.traces") == {"io", "scan"}
    assert seen == {f"coxswain.{name}": {"properties"} for name in lake_sync.HISTORY}


@needs_lake
def test_a_lease_held_by_another_host_skips_without_syncing(env, monkeypatch, capsys) -> None:
    from agent_tools import lake_sync, store_cli

    _, argv = env
    monkeypatch.setattr(lake_sync, "sync", lambda *args, **kwargs: pytest.fail("synced"))
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *args, **kwargs: store_cli.LeaseRefused(3, "other"))
    rc, out = _run(argv, capsys)
    assert rc == 0
    assert out == "lake: sync skipped (held by other)\n"
    rc, out = _run([*argv, "--json"], capsys)
    assert rc == 0
    assert json.loads(out) == {"status": "held", "holder": "other"}


@needs_lake
def test_json_reports_the_tables_and_traces(env, capsys) -> None:
    _, argv = env
    rc, out = _run([*argv, "--json"], capsys)
    report = json.loads(out)
    assert rc == 0
    assert report["new_tables"] == []
    assert {t["table"]: t["rows_appended"] for t in report["tables"]}["runs"] == 2
    assert report["traces"] == {"found": 0, "registered": 0, "skipped": 0, "would_register": 0}


@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_a_hidden_extra_exits_non_zero_naming_the_extra_on_a_real_and_a_dry_run(env, capsys, monkeypatch, dry_run) -> None:
    tmp_path, argv = env
    monkeypatch.setitem(sys.modules, "pyiceberg.catalog.sql", None)
    rc, out = _run([*argv, *dry_run], capsys)
    assert rc == 2
    assert "optional extra `lake`" in out
    assert "Traceback" not in out
    assert not (tmp_path / "lake-catalog.db").exists()


@needs_lake
@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_a_missing_pyarrow_exits_non_zero_naming_the_extra_and_json_stays_json(env, capsys, monkeypatch, dry_run) -> None:
    _, argv = env
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.delitem(sys.modules, "agent_tools.lake_sync", raising=False)
    monkeypatch.delattr(agent_tools, "lake_sync", raising=False)
    rc, out = _run([*argv, *dry_run, "--json"], capsys)
    assert rc == 2
    assert json.loads(out) == {
        "error": "the Iceberg lake needs the optional extra `lake`; pyarrow is missing: pip install 'coxswain-tools[lake]'"
    }


@pytest.mark.parametrize(
    "routing, provider, expected",
    [
        ("provider_profile: {provider}\n  nested: y\n", None, "lake: profile unreadable"),
        ("provider_profile: {tmp}/missing.yaml\n", None, "lake: provider profile not readable"),
        ("provider_profile: {provider}\n", "- a\n- b\n", "lake: provider profile is not a YAML mapping"),
    ],
)
def test_a_named_profile_that_is_unusable_is_refused_instead_of_falling_back_to_a_default_lake(
    tmp_path, capsys, routing, provider, expected
) -> None:
    (tmp_path / "provider.yaml").write_text(provider or "")
    (tmp_path / "profile.yaml").write_text(routing.format(provider=tmp_path / "provider.yaml", tmp=tmp_path))
    runs = tmp_path / "runs"
    for dry_run in ([], ["--dry-run"]):
        rc, out = _run(["lake", "sync", "--profile", str(tmp_path / "profile.yaml"), "--runs-dir", str(runs), *dry_run], capsys)
        assert rc == 2
        assert expected in out
    assert not runs.exists()


def test_an_explicit_profile_that_does_not_exist_is_refused(tmp_path, capsys) -> None:
    rc, out = _run(["lake", "sync", "--profile", str(tmp_path / "nope.yaml"), "--runs-dir", str(tmp_path / "runs")], capsys)
    assert rc == 2
    assert f"lake: no profile at {tmp_path / 'nope.yaml'}" in out


@needs_lake
def test_no_profile_at_all_falls_back_to_the_runs_dir(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    (tmp_path / "runs").mkdir()
    _seed_store(tmp_path / "runs" / "cox.db")
    rc, out = _run(["lake", "sync", "--runs-dir", str(tmp_path / "runs"), "--dry-run"], capsys)
    assert rc == 0
    assert f"runs: would append 2 (mark {E2})" in out
    assert not (tmp_path / "runs" / "lake").exists()


def test_the_report_redacts_every_url_and_words_a_dry_run_as_would() -> None:
    results = [SimpleNamespace(table="runs", rows_found=4, rows_appended=0, new_mark="m1")]
    report = cli._lake_sync_report(
        results, (5, 0, 2), True, ["coxswain.runs"],
        "postgresql://u:secret@h/db", "s3://bucket/lake", "s3://u:tok@bucket/traces",
    )
    assert "secret" not in json.dumps(report) and "tok@" not in json.dumps(report)
    assert cli._lake_sync_lines(report) == [
        "lake: would create coxswain.runs", "runs: would append 4 (mark m1)", "traces: would register 3, 2 skipped",
    ]

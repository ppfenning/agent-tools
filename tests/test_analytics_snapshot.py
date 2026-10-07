import logging
import sys
import types

import pytest

from agent_tools import analytics_snapshot as snap

URL = "postgresql://cox:s3cret@db.example/store"


@pytest.fixture(autouse=True)
def _reset_once(monkeypatch):
    monkeypatch.setattr(snap, "_warned", False)


def _fake_copy(calls):
    def copy(store_url, dest):
        calls.append(store_url)
        for table in snap.TABLES:
            (dest / f"{table}.parquet").write_bytes(b"")
    return copy


def test_fresh_none_is_not_fresh():
    assert snap.snapshot_is_fresh(None, 1000.0, 300) is False


def test_fresh_young_is_fresh():
    assert snap.snapshot_is_fresh(900.0, 1000.0, 300) is True


def test_fresh_equal_to_limit_is_stale():
    assert snap.snapshot_is_fresh(700.0, 1000.0, 300) is False


def test_fresh_old_is_stale():
    assert snap.snapshot_is_fresh(100.0, 1000.0, 300) is False


def test_max_age_default_when_missing():
    assert snap.max_age_s({}) == 300


def test_max_age_reads_the_profile_key():
    assert snap.max_age_s({"analytics": {"snapshot_max_age_s": 60}}) == 60


@pytest.mark.parametrize("bad", ["soon", -1, True, None, float("nan")])
def test_max_age_bad_values_give_default(bad):
    assert snap.max_age_s({"analytics": {"snapshot_max_age_s": bad}}) == 300


def test_max_age_non_mapping_section_gives_default():
    assert snap.max_age_s({"analytics": 5}) == 300


def test_snapshot_paths_names_four_parquet_files_and_meta(tmp_path):
    paths = snap.snapshot_paths(tmp_path)
    assert paths["runs"] == tmp_path / "analytics" / "runs.parquet"
    assert set(paths) == {"node_calls", "chair_actions", "runs", "task_records", "meta"}


def test_fresh_snapshot_is_reused(tmp_path):
    calls = []
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy(calls))
    snap.refresh_snapshot(tmp_path, URL, 1100.0, 300, copy=_fake_copy(calls))
    assert len(calls) == 1


def test_stale_snapshot_is_refreshed(tmp_path):
    calls = []
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy(calls))
    snap.refresh_snapshot(tmp_path, URL, 1300.0, 300, copy=_fake_copy(calls))
    assert len(calls) == 2
    assert snap._read_built_at(snap.snapshot_paths(tmp_path)["meta"]) == 1300.0


def test_failed_copy_leaves_no_temp_directory_and_no_snapshot(tmp_path):
    def boom(store_url, dest):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=boom)
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def _no_duckdb(monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)


def test_import_failure_returns_fallback_and_warns_once(monkeypatch, tmp_path, caplog):
    _no_duckdb(monkeypatch)
    kwargs = {"runs_dir": tmp_path, "store_url": URL, "now": 1.0, "max_age_s": 300, "fallback": lambda: [(7,)]}
    with caplog.at_level(logging.WARNING, logger=snap.__name__):
        first = snap.query("SELECT 1", (), **kwargs)
        second = snap.query("SELECT 1", (), **kwargs)
    assert (first, second) == ([(7,)], [(7,)])
    assert len(caplog.records) == 1


def test_refresh_failure_warning_carries_no_password_or_raw_url(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace())

    def boom(*args, **kwargs):
        raise RuntimeError(f"connection to '{URL}' failed: password s3cret rejected")

    monkeypatch.setattr(snap, "refresh_snapshot", boom)
    with caplog.at_level(logging.WARNING, logger=snap.__name__):
        rows = snap.query(
            "SELECT 1", (), runs_dir=tmp_path, store_url=URL, now=1.0, max_age_s=300, fallback=lambda: [(1,)]
        )
    text = caplog.records[0].getMessage()
    assert rows == [(1,)]
    assert "RuntimeError" in text
    assert "s3cret" not in text
    assert URL not in text


def test_redact_is_applied_to_a_url_that_is_not_the_store_url(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace())

    def boom(*args, **kwargs):
        raise RuntimeError("replica postgresql://rep:hunter2@elsewhere/db refused")

    monkeypatch.setattr(snap, "refresh_snapshot", boom)
    with caplog.at_level(logging.WARNING, logger=snap.__name__):
        snap.query("SELECT 1", (), runs_dir=tmp_path, store_url=URL, now=1.0, max_age_s=300, fallback=lambda: [])
    text = caplog.records[0].getMessage()
    assert "hunter2" not in text
    assert "postgresql://rep:***@elsewhere/db" in text


def test_query_runs_sql_over_the_four_tables_by_name(tmp_path):
    duckdb = pytest.importorskip("duckdb")

    def copy(store_url, dest):
        con = duckdb.connect()
        for table in snap.TABLES:
            con.execute(f"COPY (SELECT 1 AS id, '{table}' AS name) TO '{dest / (table + '.parquet')}' (FORMAT PARQUET)")
        con.close()

    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=copy)
    rows = snap.query(
        "SELECT r.name, n.id FROM runs r JOIN node_calls n ON n.id = r.id WHERE r.id = ?",
        [1],
        runs_dir=tmp_path,
        store_url=URL,
        now=1100.0,
        max_age_s=300,
        fallback=lambda: [("fallback",)],
    )
    assert rows == [("runs", 1)]


def _generations(root):
    return sorted(p.name for p in root.glob(snap.GENERATION_PREFIX + "*"))


def test_refresh_points_analytics_at_the_new_generation_and_removes_the_one_it_replaced(tmp_path):
    for now in (1000.0, 1300.0, 1600.0):
        snap.refresh_snapshot(tmp_path, URL, now, 300, copy=_fake_copy([]))
    assert (tmp_path / "analytics").is_symlink()
    assert (tmp_path / "analytics" / snap.META_FILE).exists()
    assert len(_generations(tmp_path)) == 1
    assert snap._read_built_at(snap.snapshot_paths(tmp_path)["meta"]) == 1600.0


def test_a_refresh_inside_another_refreshs_copy_does_not_delete_the_outer_generation(tmp_path):
    def outer_copy(store_url, dest):
        snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
        _fake_copy([])(store_url, dest)

    snap.refresh_snapshot(tmp_path, URL, 1010.0, 300, copy=outer_copy)
    current = (tmp_path / "analytics").resolve()
    assert current.is_dir()
    assert (current / "runs.parquet").exists()
    assert snap._read_built_at(snap.snapshot_paths(tmp_path)["meta"]) == 1010.0
    assert _generations(tmp_path) == [current.name]


def test_a_real_analytics_directory_is_migrated_to_a_generation(tmp_path):
    (tmp_path / "analytics").mkdir()
    (tmp_path / "analytics" / "old.txt").write_text("x", encoding="utf-8")
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
    assert (tmp_path / "analytics").is_symlink()
    assert not (tmp_path / "analytics" / "old.txt").exists()
    assert snap._read_built_at(snap.snapshot_paths(tmp_path)["meta"]) == 1000.0
    assert len(_generations(tmp_path)) == 1


def test_a_failed_refresh_restores_a_real_analytics_directory(tmp_path):
    (tmp_path / "analytics").mkdir()
    (tmp_path / "analytics" / "old.txt").write_text("x", encoding="utf-8")

    def boom(store_url, dest):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=boom)
    assert (tmp_path / "analytics" / "old.txt").exists()
    assert _generations(tmp_path) == []


def test_failed_refresh_leaves_the_previous_snapshot_whole(tmp_path):
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
    before = _generations(tmp_path)

    def boom(store_url, dest):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        snap.refresh_snapshot(tmp_path, URL, 2000.0, 300, copy=boom)
    assert _generations(tmp_path) == before
    assert snap._read_built_at(snap.snapshot_paths(tmp_path)["meta"]) == 1000.0


class _Con:
    """Records every statement; `fetchall` returns the rows given at construction."""

    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def _fake_duckdb(monkeypatch, rows=()):
    log = []
    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace(connect=lambda: _Con(log, list(rows))))
    return log


def test_duckdb_copy_attaches_read_only_and_copies_each_table_to_parquet(monkeypatch, tmp_path):
    log = _fake_duckdb(monkeypatch)
    snap._duckdb_copy(URL, tmp_path)
    statements = [sql for sql, _ in log]
    assert statements[:2] == ["INSTALL postgres", "LOAD postgres"]
    assert statements[2] == f"ATTACH '{URL}' AS pg (TYPE postgres, READ_ONLY)"
    assert statements[3] == (
        f"COPY (SELECT * FROM pg.public.node_calls) TO '{tmp_path}/node_calls.parquet' (FORMAT PARQUET)"
    )
    assert len(statements) == 3 + len(snap.TABLES)


def test_query_creates_a_view_per_table_and_binds_params(monkeypatch, tmp_path):
    log = _fake_duckdb(monkeypatch, rows=[[1, "a"]])
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
    rows = snap.query(
        "SELECT id, name FROM runs WHERE id = ?",
        [1],
        runs_dir=tmp_path,
        store_url=URL,
        now=1100.0,
        max_age_s=300,
        fallback=lambda: [("fallback",)],
    )
    runs_parquet = tmp_path / "analytics" / "runs.parquet"
    assert rows == [(1, "a")]
    assert [sql for sql, _ in log][:4] == [
        f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{tmp_path / 'analytics' / (t + '.parquet')}')"
        for t in snap.TABLES
    ]
    assert log[-1] == ("SELECT id, name FROM runs WHERE id = ?", [1])
    assert runs_parquet.parent.is_symlink()


def test_an_unreadable_snapshot_returns_the_fallback_not_an_io_error(monkeypatch, tmp_path, caplog):
    class Unreadable(_Con):
        def execute(self, sql, params=None):
            if sql.startswith("CREATE VIEW"):
                raise OSError("no such file")
            return super().execute(sql, params)

    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace(connect=lambda: Unreadable([], [])))
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
    with caplog.at_level(logging.WARNING, logger=snap.__name__):
        rows = snap.query(
            "SELECT 1", (), runs_dir=tmp_path, store_url=URL, now=1100.0, max_age_s=300, fallback=lambda: [(9,)]
        )
    assert rows == [(9,)]
    assert len(caplog.records) == 1


def test_sql_errors_are_not_swallowed_into_the_fallback(monkeypatch, tmp_path):
    class Bad(_Con):
        def execute(self, sql, params=None):
            if sql.startswith("SELECT"):
                raise ValueError("bad sql")
            return super().execute(sql, params)

    monkeypatch.setitem(sys.modules, "duckdb", types.SimpleNamespace(connect=lambda: Bad([], [])))
    snap.refresh_snapshot(tmp_path, URL, 1000.0, 300, copy=_fake_copy([]))
    with pytest.raises(ValueError, match="bad sql"):
        snap.query(
            "SELECT nope", (), runs_dir=tmp_path, store_url=URL, now=1100.0, max_age_s=300, fallback=lambda: []
        )

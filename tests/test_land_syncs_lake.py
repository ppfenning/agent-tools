import importlib.util
from argparse import Namespace
from pathlib import Path

import pytest

from agent_tools import cli, store_cli

CATALOG = Path("/lake/catalog.db")


@pytest.mark.parametrize("rc, reached, extra, resolved, path, exists, expected", [
    (0, ["cherry_pick", "mark_done"], True, True, CATALOG, True, True),
    (0, ["mark_done"], True, True, None, False, True),
    (1, ["mark_done"], True, True, CATALOG, True, False),
    (0, ["cherry_pick", "merge"], True, True, CATALOG, True, False),
    (0, [], True, True, CATALOG, True, False),
    (0, ["mark_done"], False, True, CATALOG, True, False),
    (0, ["mark_done"], True, False, CATALOG, True, False),
    (0, ["mark_done"], True, True, CATALOG, False, False),
])
def test_should_sync_lake_row(rc, reached, extra, resolved, path, exists, expected):
    assert cli.should_sync_lake(rc, reached, extra, resolved, path, exists) is expected


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """A routing and a provider profile naming a SQLite catalog that already exists, and a patched sync core."""
    for name in ("pyiceberg", "pyarrow"):
        if importlib.util.find_spec(name) is None:
            pytest.skip("the optional extra `lake` is not installed")
    from agent_tools import lake_config, lake_sync, lake_tables, lake_traces

    (tmp_path / "lake-catalog.db").touch()
    provider = tmp_path / "provider.yaml"
    provider.write_text(
        f"storage_url: sqlite:///{tmp_path}/store.db\nlake_catalog_url: sqlite:///{tmp_path}/lake-catalog.db\n"
        f"lake_url: file://{tmp_path}/lake\n"
    )
    routing = tmp_path / "profile.yaml"
    routing.write_text(f"provider_profile: {provider}\n")
    monkeypatch.setattr(lake_config, "load_catalog", lambda config: object())
    monkeypatch.setattr(lake_tables, "ensure_tables", lambda catalog: None)
    monkeypatch.setattr(lake_traces, "register_traces", lambda catalog, root, dry_run=False: (5, 2, 3))
    return Namespace(profile=str(routing)), tmp_path, lake_sync


def _results(*counts):
    from agent_tools.lake_sync import SyncResult

    return [SyncResult(f"t{i}", n, n, None, "m") for i, n in enumerate(counts)]


def test_a_land_that_closed_its_item_prints_the_rows_and_trace_files_synced(lake, monkeypatch, capsys):
    a, tmp_path, lake_sync = lake
    monkeypatch.setattr(lake_sync, "sync", lambda catalog, store_url, dry_run=False: _results(3, 4))
    cli._lake_after_land(a, tmp_path / "runs", 0, ["cherry_pick", "mark_done"])
    assert capsys.readouterr().out == "lake: +7 rows, +2 trace files\n"


def test_a_sync_that_raises_prints_one_line_and_does_not_raise(lake, monkeypatch, capsys):
    a, tmp_path, lake_sync = lake

    def boom(catalog, store_url, dry_run=False):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(lake_sync, "sync", boom)
    cli._lake_after_land(a, tmp_path / "runs", 0, ["mark_done"])
    assert capsys.readouterr().out == "lake: sync skipped (RuntimeError)\n"


def test_a_lease_held_by_another_host_skips_without_raising(lake, monkeypatch, capsys):
    a, tmp_path, lake_sync = lake
    monkeypatch.setattr(lake_sync, "sync", lambda catalog, store_url, dry_run=False: pytest.fail("synced"))
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *args, **kwargs: store_cli.LeaseRefused(3, "other"))
    cli._lake_after_land(a, tmp_path / "runs", 0, ["mark_done"])
    assert capsys.readouterr().out == "lake: sync skipped (held by other)\n"


def test_a_lake_nobody_created_stays_uncreated(lake, monkeypatch, capsys):
    a, tmp_path, lake_sync = lake
    (tmp_path / "lake-catalog.db").unlink()
    monkeypatch.setattr(lake_sync, "sync", lambda catalog, store_url, dry_run=False: pytest.fail("synced"))
    cli._lake_after_land(a, tmp_path / "runs", 0, ["mark_done"])
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "lake-catalog.db").exists()

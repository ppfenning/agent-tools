from __future__ import annotations

import dataclasses

from agent_tools import cli

TWELVE = {
    "lease", "docket", "approved", "quarantined", "stranded", "attempts",
    "live_initiatives", "intake", "work_store_ready", "sources_configured", "run_id", "record",
}


def test_the_real_deps_hold_no_unwired_source_for_any_of_the_twelve_names(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    fields = {
        f.name: getattr(bundle, f.name)
        for bundle in (deps.facts_deps, deps.exec_deps)
        for f in dataclasses.fields(bundle)
    }
    assert fields.keys() >= TWELVE
    assert [name for name in TWELVE if isinstance(fields[name], cli._ChairUnwired)] == []


def test_the_recorder_is_built_with_a_store_runner_and_the_lease_holder(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.chair_read_record, "recorder", lambda *args, **kwargs: seen.update(kwargs))
    cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert seen["holder"] == cli.chair.lease_holder("chair", 1, "h")
    assert callable(seen["store"])


def test_the_dispatch_facts_offer_the_lane_hosts_named_in_the_profile_file(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: jarvis\n    ssh: jarvis\n    workspace_dir: /w\n", encoding="utf-8")
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0}],
    }


def test_a_live_pidfile_run_with_no_store_lane_still_counts_as_a_local_lane(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.pid").write_text("123", encoding="utf-8")
    monkeypatch.setattr("agent_tools.epic.run_live", lambda *args, **kwargs: True)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {"max_in_flight": 3, "live_runs": 1, "hosts": []}  # type: ignore[misc]


def test_lanes_on_an_unlisted_host_and_live_pidfiles_all_count_as_local() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in ("jarvis", "elsewhere", "omarchy")]
    assert cli._dispatch_counts(lanes, "omarchy", ["jarvis"], 2) == {"": 4, "jarvis": 1}


def test_live_lanes_count_per_host_with_the_local_machine_under_the_empty_name() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in (None, "omarchy", "jarvis")]
    assert cli._live_by_host(lanes, "omarchy") == {"": 2, "jarvis": 1}


def test_the_deps_weekly_reader_passes_a_store_spend_that_reads_the_store_from_since(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(cli.run_store, "cost_since", lambda runs_dir, since: (runs_dir, since))
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    deps.facts_deps.weekly()
    assert seen["store_spend"]("2026-08-29") == (runs, "2026-08-29")

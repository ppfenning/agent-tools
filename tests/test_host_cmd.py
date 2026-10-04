from __future__ import annotations

import argparse
import datetime

from agent_tools import cli, host_cmd, run_store
from agent_tools.lane_hosts import LaneHost

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
JARVIS = {"name": "jarvis", "ssh": "jarvis", "capacity": 8, "state": "active",
          "beat_at": "2026-09-26T11:58:00+00:00", "versions_json": '{"login_ok": true}'}


def test_only_active_rows_are_lane_hosts_and_the_workspace_comes_from_the_profile_host():
    rows = [JARVIS, {**JARVIS, "name": "pi", "ssh": "pi", "state": "draining"}, {**JARVIS, "name": "old", "state": "offline"}]
    assert host_cmd.host_rows_to_lane_hosts(rows, [LaneHost("jarvis", "x", "/w")]) == (LaneHost("jarvis", "jarvis", "/w"),)


def test_the_host_list_line_has_state_capacity_beat_age_and_login():
    assert host_cmd.format_host_list([JARVIS], NOW) == ["jarvis  active  cap 8  beat 2m ago  login ok"]


def test_row_weights_and_capabilities_read_active_rows_that_carry_them():
    rows = [
        {**JARVIS, "weight": 2, "capabilities": ["go"]},
        {**JARVIS, "name": "pi", "weight": 3, "capabilities": "go,rust"},
        {**JARVIS, "name": "old", "capabilities": '["go"]'},
        {**JARVIS, "name": "gone", "state": "draining", "weight": 5, "capabilities": ["go"]},
    ]
    assert host_cmd.row_weights(rows) == {"jarvis": 2, "pi": 3}
    assert host_cmd.row_capabilities(rows) == {"jarvis": ["go"], "pi": ["go", "rust"], "old": ["go"]}


def test_a_row_from_a_store_with_no_weight_or_capabilities_column_yields_neither():
    assert (host_cmd.row_weights([JARVIS]), host_cmd.row_capabilities([JARVIS])) == ({}, {})


def test_add_argv_with_default_weight_and_no_capabilities_matches_host_upsert_exactly():
    assert host_cmd.add_argv("jarvis", "jarvis", 8, 1, "", "chair") == [
        "host", "upsert", "jarvis", "--ssh", "jarvis", "--capacity", "8", "--by", "chair",
    ]


def test_capacity_upsert_argv_changes_only_capacity_and_never_sends_weight_capabilities_or_state():
    row = {"name": "jarvis", "ssh": "jarvis.tail", "state": "draining", "weight": 5, "capabilities": ["go", "rust"]}
    argv = host_cmd.capacity_upsert_argv(row, 0, "chair")
    assert argv == ["host", "upsert", "jarvis", "--ssh", "jarvis.tail", "--capacity", "0", "--by", "chair"]
    assert "--weight" not in argv and "--capabilities" not in argv


def test_local_host_missing_is_true_only_when_no_row_names_the_host():
    assert host_cmd.local_host_missing([], "omarchy") is True
    assert host_cmd.local_host_missing([{"name": "omarchy"}], "omarchy") is False


def test_local_host_add_argv_uses_the_hostname_as_its_own_ssh_destination():
    assert host_cmd.local_host_add_argv("omarchy", 8, "chair") == [
        "host", "upsert", "omarchy", "--ssh", "omarchy", "--capacity", "8", "--by", "chair",
    ]


def test_a_host_that_never_beat_reads_never_and_login_unknown():
    row = {**JARVIS, "beat_at": None, "versions_json": None}
    assert host_cmd.format_host_list([row], NOW) == ["jarvis  active  cap 8  beat never  login ?"]


def test_versions_report_keeps_a_missing_claude_as_null():
    assert host_cmd.versions_report("0.20.0", "v1", "v2", None, None)["claude"] is None


def test_a_draining_host_with_no_live_lane_reads_drained():
    row = {**JARVIS, "state": "draining"}
    assert host_cmd.format_host_list([row], NOW, {"jarvis": 0}) == ["jarvis  drained  cap 8  beat 2m ago  login ok"]


def test_a_draining_host_with_a_live_lane_still_reads_draining():
    row = {**JARVIS, "state": "draining"}
    assert host_cmd.format_host_list([row], NOW, {"jarvis": 1}) == ["jarvis  draining  cap 8  beat 2m ago  login ok"]


def test_an_active_host_is_unaffected_by_live_by_host():
    assert host_cmd.format_host_list([JARVIS], NOW, {"jarvis": 0}) == ["jarvis  active  cap 8  beat 2m ago  login ok"]


def test_a_draining_row_is_never_a_lane_host_no_matter_how_many_lanes_it_still_has():
    # host_rows_to_lane_hosts takes no live-lane count: a draining row is dropped on `state` alone,
    # the guarantee that already keeps a draining host from receiving new lanes.
    row = {**JARVIS, "state": "draining"}
    assert host_cmd.host_rows_to_lane_hosts([row]) == ()


def test_the_local_row_is_never_a_lane_host_candidate():
    rows = [JARVIS, {**JARVIS, "name": "omarchy", "ssh": "omarchy"}]
    assert host_cmd.host_rows_to_lane_hosts(rows, [LaneHost("jarvis", "x", "/w")], local="omarchy") == (
        LaneHost("jarvis", "jarvis", "/w"),
    )


def test_the_local_rows_workspace_dir_resolves_to_the_profiles_own_instead_of_refusing():
    rows = [{**JARVIS, "name": "omarchy", "versions_json": None}]
    assert host_cmd.local_workspace_dir(rows, "omarchy", "/srv/ws") == "/srv/ws"


def test_the_host_list_counts_this_machines_own_lanes_under_its_own_row_name():
    lanes = [run_store.Lane("r1", None, "t", "t"), run_store.Lane("r2", "jarvis", "t", "t"), run_store.Lane("r3", "pi", "t", "t")]
    live = cli._live_by_host_name(lanes, "jarvis")
    assert live == {"jarvis": 2, "pi": 1}
    assert host_cmd.format_host_list([{**JARVIS, "state": "draining"}], NOW, live) == ["jarvis  draining  cap 8  beat 2m ago  login ok"]


def test_a_gather_that_cannot_run_is_null_and_never_raises():
    def run(argv: list[str]) -> tuple[int, str]:
        raise OSError("no such tool")

    result = host_cmd.beat_versions(run, "0.20.0", "/h", None)
    host_facts = result.pop("host_facts")
    assert result == {
        "cox": "0.20.0", "graphs": None, "cartridges": None, "claude": None, "login_ok": None,
        "workspace_dir": None, "repos": [], "checkouts": {},
    }
    assert isinstance(host_facts["os"], str) and host_facts["os"]
    assert isinstance(host_facts["cpu_count"], int)
    assert isinstance(host_facts["mem_total_gb"], float)


def test_the_beat_reads_login_from_claude_auth_status_json():
    def run(argv: list[str]) -> tuple[int, str]:
        if argv[:2] == ["claude", "auth"]:
            return 0, '{"loggedIn": true}'
        if "rev-parse" in argv:
            return (0, "feature-x\n") if argv[2] == "/h" else (1, "")
        if "rev-list" in argv:
            return (0, "4\n") if argv[2] == "/h" else (1, "")
        return 0, "v9\n"

    result = host_cmd.beat_versions(run, None, "/h", "/c", "/srv/ws", ["/h", "/c", "/t"])
    result.pop("host_facts")
    assert result == {
        "cox": "v9", "graphs": "v9", "cartridges": "v9", "claude": "v9", "login_ok": True,
        "workspace_dir": "/srv/ws", "repos": ["/h", "/c", "/t"],
        "checkouts": {
            "h": {"branch": "feature-x", "behind_main": 4},
            "c": {"branch": "", "behind_main": 0},
            "t": {"branch": "", "behind_main": 0},
        },
    }


def test_checkout_detail_reads_branch_and_commits_behind_main_through_run_with_no_fetch():
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (0, "main\n") if argv[-1] == "HEAD" else (0, "2\n")

    assert host_cmd._checkout_detail(run, "/srv/coxswain-tools") == ("coxswain-tools", {"branch": "main", "behind_main": 2})
    assert all(c[:2] != ["git", "fetch"] and "fetch" not in c for c in calls)


def test_checkout_detail_reads_branch_empty_and_behind_main_zero_when_a_repo_fails():
    def run(argv: list[str]) -> tuple[int, str]:
        return 128, "fatal: not a git repository"

    assert host_cmd._checkout_detail(run, "/srv/missing") == ("missing", {"branch": "", "behind_main": 0})


def test_a_table_only_host_takes_the_workspace_its_own_beat_recorded_and_becomes_dispatchable():
    beaten = {**JARVIS, "versions_json": '{"workspace_dir": "/srv/ws"}'}
    fresh = {**JARVIS, "name": "fresh", "ssh": "fresh", "versions_json": None}
    hosts = host_cmd.host_rows_to_lane_hosts([beaten, fresh])
    assert hosts == (LaneHost("jarvis", "jarvis", "/srv/ws"), LaneHost("fresh", "fresh", ""))
    assert host_cmd.dispatchable(hosts) == (["jarvis"], ["fresh"])


def test_a_postgres_row_with_jsonb_versions_and_a_datetime_beat_reads_like_a_sqlite_one():
    row = {**JARVIS, "beat_at": datetime.datetime(2026, 9, 26, 11, 58, tzinfo=datetime.UTC), "versions_json": {"login_ok": True}}
    assert host_cmd.format_host_list([row], NOW) == ["jarvis  active  cap 8  beat 2m ago  login ok"]


def test_profile_hosts_the_table_does_not_name_are_shadowed():
    assert host_cmd.shadowed([LaneHost("jarvis", "j", "/w"), LaneHost("pi", "p", "/w")], ["jarvis"]) == ["pi"]


def test_sync_uses_the_paths_the_hosts_own_beat_recorded():
    assert host_cmd.recorded_repos({"versions_json": '{"repos": ["/srv/graphs", "rel", 3]}'}) == ["/srv/graphs"]
    assert host_cmd.recorded_repos(None) == []


def test_sync_names_a_checkout_missing_on_the_host_apart_from_a_failed_pull():
    def run(argv: list[str]) -> tuple[int, str]:
        return 128, "fatal: cannot change to '/x/graphs': No such file or directory"

    assert host_cmd.sync_host("jarvis", ["/x/graphs"], run) == (
        1, ["graphs  not updated (no checkout at /x/graphs on the host): fatal: cannot change to '/x/graphs': No such file or directory"],
    )


def test_sync_reports_a_pull_that_is_not_a_fast_forward_and_does_not_force():
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (128, "fatal: Not possible to fast-forward, aborting.") if "/h" in argv[-1] else (0, "abc1234\n")

    assert host_cmd.sync_host("jarvis", ["/h", "/c"], run) == (
        1, ["h  not updated (not a fast-forward): fatal: Not possible to fast-forward, aborting.", "c  abc1234"],
    )
    assert all("--force" not in " ".join(c) for c in calls)


def test_cox_host_add_runs_store_cli_host_upsert_with_the_holder_label(tmp_path, monkeypatch, capsys):
    (tmp_path / "ws").mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_runner(runs_dir):
        def run(argv: list[str]) -> tuple[int, str]:
            seen.append(argv)
            return 0, '{"name": "jarvis"}'

        return run

    monkeypatch.setattr(cli.store_cli, "runner", fake_runner)
    monkeypatch.setenv("COX_SESSION_LABEL", "chair")
    args = argparse.Namespace(profile=str(profile), name="jarvis", ssh="jarvis", capacity=8, weight=1, capabilities="")
    assert cli._host_add(args) == 0
    assert seen == [["host", "upsert", "jarvis", "--ssh", "jarvis", "--capacity", "8", "--by", "chair"]]
    assert capsys.readouterr().out == '{"name": "jarvis"}\n'


def test_cox_host_add_refuses_a_non_default_weight_before_calling_the_store(tmp_path, monkeypatch, capsys):
    (tmp_path / "ws").mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: (lambda argv: seen.append(argv) or (0, "{}")))
    args = argparse.Namespace(profile=str(profile), name="jarvis", ssh="jarvis", capacity=8, weight=2, capabilities="")
    assert cli._host_add(args) == 1
    assert seen == []
    assert capsys.readouterr().out == "refused jarvis: the store has no place for weight or capabilities yet\n"


def test_cox_host_add_names_the_profile_lane_hosts_the_table_now_overrides(tmp_path, monkeypatch, capsys):
    (tmp_path / "ws").mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"workspace_dir: {tmp_path / 'ws'}\nlane_hosts:\n  - name: pi\n    ssh: pi\n    workspace_dir: /w\n", encoding="utf-8",
    )
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: lambda argv: (0, "{}"))
    args = argparse.Namespace(profile=str(profile), name="jarvis", ssh="jarvis", capacity=8, weight=1, capabilities="")
    assert cli._host_add(args) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "note: profile lane_hosts not in the hosts table are not lane hosts while it has rows: pi; add them with `cox host add`"
    )


def test_cox_host_capacity_builds_the_upsert_argv_from_the_stores_own_row(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)
    (tmp_path / "ws").mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    row = {"name": "jarvis", "ssh": "jarvis", "capacity": 8, "state": "active", "weight": 1, "capabilities": None}
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [row])
    calls: list[list[str]] = []

    def fake_runner(runs_dir):
        def run(argv: list[str]) -> tuple[int, str]:
            calls.append(argv)
            return 0, "{}"

        return run

    monkeypatch.setattr(cli.store_cli, "runner", fake_runner)
    args = argparse.Namespace(profile=str(profile), name="jarvis", n=0, json=False)

    assert cli._host_capacity(args) == 0
    assert calls == [host_cmd.capacity_upsert_argv(row, 0, "unlabeled")]
    assert "--capacity" in calls[0] and calls[0][calls[0].index("--capacity") + 1] == "0"
    assert "--ssh" in calls[0] and calls[0][calls[0].index("--ssh") + 1] == "jarvis"


def test_cox_host_capacity_refuses_a_host_absent_from_the_table(tmp_path, monkeypatch, capsys):
    (tmp_path / "ws").mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [])

    def _refuse_runner(runs_dir):
        raise AssertionError("store_cli must not run for a host absent from the table")

    monkeypatch.setattr(cli.store_cli, "runner", _refuse_runner)
    args = argparse.Namespace(profile=str(profile), name="ghost", n=4, json=False)

    code = cli._host_capacity(args)

    assert code == 1
    assert capsys.readouterr().out.strip() == "refused ghost: no such host"


def _on(tmp_path, versions_json):
    import sqlite3

    (tmp_path / "ws" / "runs").mkdir(parents=True)
    conn = sqlite3.connect(tmp_path / "ws" / "runs" / "cox.db")
    conn.execute("CREATE TABLE hosts (name TEXT, ssh TEXT, capacity INTEGER, state TEXT, versions_json TEXT)")
    conn.execute("INSERT INTO hosts VALUES ('jarvis', 'jarvis', 8, 'active', ?)", (versions_json,))
    conn.commit()
    conn.close()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    args = argparse.Namespace(profile=str(profile), on="jarvis", tier_ceiling=None, effort_ceiling=None, fix_attempts=None)
    return cli._lane_host_or_refuse(args, {"workspace_dir": str(tmp_path / "ws")})


def test_route_launch_on_accepts_a_table_only_host_once_it_has_beaten_with_its_workspace(tmp_path):
    assert _on(tmp_path, '{"workspace_dir": "/srv/ws"}') == (LaneHost("jarvis", "jarvis", "/srv/ws"), None)


def test_route_launch_on_refuses_a_table_only_host_that_never_beat_and_says_how_to_fix_it(tmp_path, capsys):
    assert _on(tmp_path, None) == (None, 2)
    assert capsys.readouterr().out == (
        "routing: host jarvis has no workspace_dir: run `cox host beat jarvis` on it, or list it under lane_hosts in the profile\n"
    )


def test_run_store_hosts_reads_rows_by_name_and_is_empty_with_no_store_or_table(tmp_path):
    import sqlite3

    from agent_tools import run_store

    assert run_store.hosts(tmp_path) == []
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT)")
    assert run_store.hosts(tmp_path) == []
    conn.execute("CREATE TABLE hosts (name TEXT, ssh TEXT, capacity INTEGER, state TEXT)")
    conn.executemany("INSERT INTO hosts VALUES (?, ?, ?, ?)", [("pi", "pi", 2, "active"), ("jarvis", "jarvis", 8, "active")])
    conn.commit()
    conn.close()
    assert [r["name"] for r in run_store.hosts(tmp_path)] == ["jarvis", "pi"]


def _remove_args(tmp_path, monkeypatch, state, lanes, dry_run=False):
    (tmp_path / "ws").mkdir(exist_ok=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: [{**JARVIS, "state": state}])
    monkeypatch.setattr(cli.run_store, "live_lanes", lambda runs_dir, now: lanes)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.store_cli, "runner", lambda runs_dir: lambda argv: seen.append(argv) or (0, "{}"))
    return argparse.Namespace(profile=str(profile), name="jarvis", dry_run=dry_run), seen


def test_cox_host_remove_deletes_a_draining_idle_host_and_prints_the_row(tmp_path, monkeypatch, capsys):
    args, seen = _remove_args(tmp_path, monkeypatch, "draining", [])
    assert cli._host_remove(args) == 0
    assert seen == [["host", "delete", "jarvis", "--by", "unlabeled"]]
    assert capsys.readouterr().out.splitlines()[-2:] == [
        'removed jarvis: {"beat_at": "2026-09-26T11:58:00+00:00", "capacity": 8, "name": "jarvis", "ssh": "jarvis", '
        '"state": "draining", "versions_json": "{\\"login_ok\\": true}"}',
        "  restore: cox host add jarvis --ssh jarvis --capacity 8",
    ]


def test_cox_host_remove_refuses_an_active_host_and_names_host_drain(tmp_path, monkeypatch, capsys):
    args, seen = _remove_args(tmp_path, monkeypatch, "active", [])
    assert cli._host_remove(args) == 1
    assert seen == []
    assert capsys.readouterr().out == "refused jarvis: active; run `cox host drain jarvis` first\n"


def test_cox_host_remove_refuses_a_draining_host_with_a_live_lane(tmp_path, monkeypatch, capsys):
    args, seen = _remove_args(tmp_path, monkeypatch, "draining", [run_store.Lane("r1", "jarvis", "t", "t")])
    assert cli._host_remove(args) == 1
    assert seen == []
    assert capsys.readouterr().out == "refused jarvis: live run r1 still on it; wait for it to finish\n"


def test_cox_host_remove_dry_run_deletes_nothing(tmp_path, monkeypatch, capsys):
    args, seen = _remove_args(tmp_path, monkeypatch, "draining", [], dry_run=True)
    assert cli._host_remove(args) == 0
    assert seen == []
    assert capsys.readouterr().out.splitlines()[-1] == "  restore: cox host add jarvis --ssh jarvis --capacity 8"


def test_removed_line_restores_a_null_ssh_row_with_its_name():
    row = {"name": "pi", "ssh": None, "capacity": 2, "state": "draining"}
    assert host_cmd.removed_line(row).splitlines()[-1] == "  restore: cox host add pi --ssh pi --capacity 2"

import json
import shutil
import subprocess

import pytest
from test_route_cli import _init_repo, _unmeasured_window, _wait_for, _write_harness

from agent_tools import cli, route, usage_window
from agent_tools.cli import main

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")


@pytest.fixture(autouse=True)
def _no_ccusage(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


@pytest.fixture(autouse=True)
def _logged_in_host(monkeypatch):
    """The host's `claude auth status`; ssh never runs. A test overrides it to refuse the launch."""
    monkeypatch.setattr(cli, "_host_auth_output", lambda argv: '{"loggedIn": true}')


def _setup(tmp_path):
    """A workspace with one epic, a clean repo, and a profile whose lane host `box` is a local directory."""
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    (initiative_dir / "p1").mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    (initiative_dir / "p1" / "t.md").write_text("---\nid: t\nstate: ready\n---\n\nBody\n")
    remote = tmp_path / "remote"
    (remote / "work").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"harness_dir: {harness_dir}\n"
        f"workspace_dir: {ws}\n"
        "cartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\n"
        "lane_hosts:\n"
        f"  - {{name: box, ssh: me@box, workspace_dir: {remote}}}\n"
    )
    argv = ["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir), "--repo", str(repo)]
    return ws, remote, argv


def _run_files(ws):
    """Names in runs/, without chair.json: the leader guard claims the loop before any launch."""
    return sorted(p.name for p in (ws / "runs").iterdir() if p.name != "chair.json")


def _fake_edge(monkeypatch, calls, codes=None, real_rsync=False):
    """Records every argv and returns `codes[step]`, default 0; ssh never runs, rsync runs only with `real_rsync`.

    The sync and launch steps are both an ssh argv, so `step` is "sync" or "ssh" (launch) by content, not argv[0]."""
    codes = codes or {}

    def edge(cwd):
        def run(argv):
            calls.append(argv)
            if argv[0] == "rsync":
                if real_rsync:
                    return subprocess.run(argv, cwd=cwd).returncode
                return codes.get("rsync", 0)
            if argv[0] == "ssh" and "route launch epic" not in argv[2]:
                return codes.get("sync", 0)
            return codes.get("ssh", 0)

        return run, lambda path: path

    monkeypatch.setattr(cli, "_remote_edge", edge)


def _no_local_process(monkeypatch):
    """The gates run git through Popen; only the detached harness launch is refused."""
    real = subprocess.Popen

    def guarded(*args, **kwargs):
        assert not kwargs.get("start_new_session"), "a local lane was started"
        return real(*args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "Popen", guarded)


@needs_rsync
def test_on_copies_the_initiative_starts_the_lane_and_writes_only_the_remote_record(tmp_path, monkeypatch, capsys):
    ws, remote, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls, real_rsync=True)
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box", "--label", "lbl"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run demo-1" in out and "host box" in out
    assert (remote / "work" / "demo" / "p1" / "t.md").exists()
    assert calls[2][0] == "ssh" and "--run-id demo-1" in calls[2][2] and "--label lbl" in calls[2][2]
    record = json.loads((ws / "runs" / "demo-1.remote.json").read_text())
    assert set(record) == {"host", "launched_at", "repo"} and record["host"] == "box"
    assert record["repo"] == argv[argv.index("--repo") + 1]
    assert _run_files(ws) == ["demo-1.remote.json"]


def test_on_dry_run_prints_the_rsync_and_ssh_argvs_and_starts_nothing(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box", "--no-claim", "--dry-run"])
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [line.split()[1] for line in lines if line.startswith("dry-run: ")] == ["rsync", "ssh"]
    assert "host box" in lines
    assert not [line for line in lines if line.split(" ")[0] in {"pid", "log", "trace"}]
    assert calls == []
    assert _run_files(ws) == []


def test_on_an_unknown_host_exits_non_zero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    rc = main([*argv, "--on", "nowhere"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "unknown lane host: nowhere" in out and "configured: box" in out
    assert calls == []
    assert list((ws / "runs").iterdir()) == []  # refused before the loop is claimed


def test_on_a_host_not_logged_in_refuses_before_copying_and_writes_no_remote_record(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls, asked = [], []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    monkeypatch.setattr(cli, "_host_auth_output", lambda a: asked.append(a) or '{"loggedIn": false}')
    rc = main([*argv, "--on", "box"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "routing: launch on box failed at auth: claude auth: not logged in on the host (run claude auth login there)" in out
    assert asked == [["ssh", "me@box", "claude auth status"]]
    assert calls == []
    assert _run_files(ws) == []


@pytest.mark.parametrize("failing", ["rsync", "ssh"])
def test_on_a_failed_launch_step_exits_non_zero_and_writes_no_remote_record(tmp_path, monkeypatch, capsys, failing):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls, {failing: 1})
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box"])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"failed at {failing}" in out
    assert calls[-1][0] == failing
    assert _run_files(ws) == []


@pytest.mark.parametrize("planted", [["demo-7.remote.json"], ["demo-7.log", "demo-7.pid"], ["demo-7:t.json"]])
@pytest.mark.parametrize("extra", [[], ["--on", "box"]])
def test_run_id_whose_files_are_in_runs_is_refused_and_nothing_is_overwritten(tmp_path, monkeypatch, capsys, extra, planted):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    for name in planted:
        (ws / "runs" / name).write_text("before")
    rc = main([*argv, "--run-id", "demo-7", *extra])
    out = capsys.readouterr().out
    assert rc == 2
    assert "run id demo-7 is already taken" in out
    assert calls == []
    assert _run_files(ws) == sorted(planted)
    assert all((ws / "runs" / name).read_text() == "before" for name in planted)


def test_a_second_on_launch_with_the_first_runs_id_is_refused_and_keeps_its_record(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    assert main([*argv, "--on", "box"]) == 0
    first = (ws / "runs" / "demo-1.remote.json").read_text()
    rc = main([*argv, "--on", "box", "--run-id", "demo-1"])
    assert rc == 2
    assert "run id demo-1 is already taken" in capsys.readouterr().out
    assert len(calls) == 3  # rsync, sync, launch on the first --on; the refused second run makes none
    assert (ws / "runs" / "demo-1.remote.json").read_text() == first


def test_two_on_launches_without_a_run_id_get_different_ids_and_keep_both_records(tmp_path, monkeypatch, capsys):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    _no_local_process(monkeypatch)
    assert main([*argv, "--on", "box"]) == 0
    first = (ws / "runs" / "demo-1.remote.json").read_text()
    assert main([*argv, "--on", "box"]) == 0
    out = capsys.readouterr().out
    assert "run demo-1" in out and "run demo-2" in out
    assert (ws / "runs" / "demo-1.remote.json").read_text() == first
    assert (ws / "runs" / "demo-2.remote.json").exists()
    launch_calls = [c for c in calls if c[0] == "ssh" and "route launch epic" in c[2]]
    assert launch_calls[1][2].count("--run-id demo-2") == 1


def test_taken_run_names_names_each_entrys_bare_run_id(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for name in ("demo-1.remote.json", "demo-2.log", "demo-3:t.json", "demo-4-trace"):
        (runs / name).write_text("")
    taken = set(cli._taken_run_names(runs))
    assert {"demo-1", "demo-2", "demo-3", "demo-4"} <= taken
    assert "demo-5" not in taken and "demo-10" not in taken


@pytest.mark.parametrize("flag", [["--tier-ceiling", "cheap"], ["--effort-ceiling", "low"], ["--fix-attempts", "1"]])
def test_on_refuses_an_option_the_remote_launch_would_drop(tmp_path, monkeypatch, capsys, flag):
    ws, _, argv = _setup(tmp_path)
    calls = []
    _fake_edge(monkeypatch, calls)
    rc = main([*argv, "--on", "box", *flag])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"--on does not carry {flag[0]}" in out
    assert calls == []
    assert list((ws / "runs").iterdir()) == []


def test_the_production_edge_pushes_to_the_ssh_location_from_the_workspace(tmp_path, monkeypatch, capsys):
    """No `_remote_edge` fake: subprocess.run is the fake ssh, so the real `host.ssh:path` form is what gets checked."""
    ws, remote, argv = _setup(tmp_path)
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    _no_local_process(monkeypatch)
    rc = main([*argv, "--on", "box", "--label", "lbl"])
    assert rc == 0, capsys.readouterr().out
    remote_calls = [(cmd, cwd) for cmd, cwd in seen if cmd[0] in ("rsync", "ssh")]
    assert remote_calls[0] == (["rsync", "-a", "--delete", "work/demo/", f"me@box:{remote}/work/demo/"], ws)
    ssh, cwd = remote_calls[2]  # remote_calls[1] is the sync step that brings the lane host's repo up to date
    assert ssh[:2] == ["ssh", "me@box"] and f"--initiative {remote}/work/demo" in ssh[2] and cwd == ws
    assert (ws / "runs" / "demo-1.remote.json").exists()


def test_run_id_that_is_free_names_the_local_run(tmp_path, capsys):
    ws, _, argv = _setup(tmp_path)
    rc = main([*argv, "--run-id", "demo-9"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run demo-9" in out
    assert _wait_for(ws / "runs" / "demo-9.pid")
    assert (ws / "runs" / "demo-9.launched.json").exists()
    assert not (ws / "runs" / "demo-9.remote.json").exists()


def test_without_the_flags_the_local_path_is_unchanged(tmp_path, capsys):
    ws, _, argv = _setup(tmp_path)
    rc = main(argv)
    assert rc == 0
    assert "run demo-1" in capsys.readouterr().out
    assert _wait_for(ws / "runs" / "demo-1.pid")
    assert (ws / "runs" / "demo-1.launched.json").exists()
    assert not (ws / "runs" / "demo-1.remote.json").exists()


def test_parse_profile_skips_the_lane_hosts_block_and_reads_the_keys_after_it():
    text = "team: acme\nlane_hosts:\n  - name: box\n    ssh: me@box\n    workspace_dir: /srv\nassume: b\n"
    assert route.parse_profile(text) == {"team": "acme", "assume": "b"}

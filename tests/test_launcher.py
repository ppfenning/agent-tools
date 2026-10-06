import datetime
import socket
from pathlib import Path

from agent_tools import chair
from agent_tools.cli import _bare_launcher_split, _launcher_argv, main


def test_launcher_argv_is_pure_and_takes_the_resolved_plugin_root_as_data():
    argv, warning = _launcher_argv(Path("/plugins/coxswain"), ["/plugins"], False, ["-r", "x"])
    assert argv == ["claude", "--plugin-dir", "/plugins/coxswain", "-r", "x"] and warning is None
    argv, warning = _launcher_argv(None, ["/plugins"], False, [])
    assert argv == ["claude"]
    assert warning == "coxswain plugin not found under ['/plugins']; starting plain claude"


def test_bare_launcher_split_leaves_an_ordinary_subcommands_own_dashdash_untouched():
    assert _bare_launcher_split(["route", "file", "--", "-hi"]) is None


class _FakeBeater:
    pid = 424242


def _fake_spawn(argv):
    return _FakeBeater()


def _profile(tmp_path: Path, skills_root: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    p = tmp_path / "profile.yaml"
    p.write_text(f"workspace_dir: {ws}\nskills_roots: [{skills_root}]\n")
    return p


def _with_plugin(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    plugin_dir = skills_root / "coxswain" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("{}")
    return skills_root


def test_print_argv_with_the_plugin_present_names_plugin_dir_and_workspace_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" in out and str(skills_root / "coxswain") in out
    assert str(tmp_path / "workspace") in out


def test_print_argv_without_the_plugin_prints_the_fallback_line_and_a_plain_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "coxswain plugin not found under" in out
    assert "--plugin-dir" not in out


def test_no_plugin_flag_omits_the_flag_even_when_the_plugin_is_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--no-plugin", "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" not in out
    assert "coxswain plugin not found" not in out


def test_extra_arguments_after_dashdash_pass_through_verbatim_with_no_dashdash_reinserted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv", "--", "-r", "resume-me"])
    out = capsys.readouterr().out
    assert rc == 0
    expected = str(["claude", "--plugin-dir", str(skills_root / "coxswain"), "-r", "resume-me"])
    assert out.splitlines()[0] == expected


def test_a_missing_profile_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    rc = main(["--profile", str(tmp_path / "nope.yaml"), "--print-argv"])
    assert rc == 2


def test_claude_not_on_path_exits_2_with_a_one_line_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    rc = main(["--profile", str(profile), "--print-argv"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "claude" in out and "PATH" in out


def test_the_profile_is_found_through_the_env_var_when_no_flag_is_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    rc = main(["--print-argv"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "--plugin-dir" in out and str(tmp_path / "workspace") in out


def test_no_arg_off_a_tty_prints_route_status_and_does_not_open_curses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("agent_tools.home_screen.main", lambda *a, **k: (_ for _ in ()).throw(AssertionError("curses opened")))
    profile = _profile(tmp_path, tmp_path / "skills")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    main(["route", "status"])
    expected = capsys.readouterr().out
    main([])
    assert capsys.readouterr().out == expected


def test_bare_cox_on_a_tty_with_no_args_takes_the_chair_and_runs_the_launcher(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", _fake_spawn)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls and calls[0][0] == "claude"
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert f"chair taken: chair-{today}" in out
    assert "chair: beating from pid 424242" in out


def test_a_live_foreign_chair_is_printed_not_stolen_and_the_launcher_still_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    now = datetime.datetime.now(datetime.UTC)
    chair.write(
        tmp_path / "workspace" / "runs",
        {
            "session": "chair-someone-else",
            "pid": 1,
            "host": socket.gethostname(),
            "taken_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "runs": [],
        },
    )
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chair: held by chair-someone-else (pid 1) on" in out
    assert "chair taken:" not in out
    assert calls and calls[0][0] == "claude"


def test_a_stale_chair_from_an_earlier_bare_cox_today_is_retaken_not_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("os.chdir", lambda path: None)
    monkeypatch.setattr("agent_tools.cli._spawn", _fake_spawn)
    calls = []
    monkeypatch.setattr("os.execvp", lambda *a: calls.append(a))
    skills_root = _with_plugin(tmp_path)
    profile = _profile(tmp_path, skills_root)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    stale_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)
    chair.write(
        tmp_path / "workspace" / "runs",
        {
            "session": f"chair-{today}",
            "pid": 999999,
            "host": socket.gethostname(),
            "taken_at": stale_at.isoformat(),
            "heartbeat_at": stale_at.isoformat(),
            "runs": [],
        },
    )
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"chair taken: chair-{today}" in out
    assert "pass --steal" not in out
    assert calls and calls[0][0] == "claude"

from pathlib import Path

import pytest

from agent_tools.cli import home_target, main, should_exec_coxtop


def test_coxtop_runs_only_on_two_terminals_with_coxtop_on_path_and_no_flags():
    assert should_exec_coxtop(True, True, True, False) is True
    assert should_exec_coxtop(False, True, True, False) is False
    assert should_exec_coxtop(True, False, True, False) is False
    assert should_exec_coxtop(True, True, False, False) is False
    assert should_exec_coxtop(True, True, True, True) is False


def test_session_print_argv_prints_the_argv_and_cwd_bare_cox_printed_before(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    skills_root = tmp_path / "skills"
    plugin_dir = skills_root / "coxswain" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text("{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {workspace}\nskills_roots: [{skills_root}]\n")
    expected = f"['claude', '--plugin-dir', '{skills_root}/coxswain']\n{workspace}\n"
    assert main(["session", "--profile", str(profile), "--print-argv"]) == 0
    assert capsys.readouterr().out == expected
    assert main(["--profile", str(profile), "--print-argv"]) == 0
    assert capsys.readouterr().out == expected


def test_session_passes_arguments_after_dashdash_to_claude(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\nskills_roots: []\n")
    assert main(["session", "--profile", str(profile), "--no-plugin", "--print-argv", "--", "-r", "resume-me"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "['claude', '-r', 'resume-me']"


def test_home_picks_coxtop_over_curses_by_the_same_decision_bare_cox_makes():
    assert home_target(True, True, True, False) == "coxtop"
    assert home_target(True, True, False, False) == "curses"
    assert home_target(False, False, True, False) == "curses"
    assert home_target(True, True, True, True) == "curses"


class _Execd(Exception):
    pass


def _terminal_with(monkeypatch, coxtop: bool) -> list:
    """Both streams are terminals; `which` finds claude, and coxtop when asked; `os.execvp` is recorded and stops the call."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: None if name == "coxtop" and not coxtop else f"/usr/bin/{name}")
    calls = []

    def _execvp(*a):
        calls.append(a)
        raise _Execd

    monkeypatch.setattr("os.execvp", _execvp)
    return calls


def test_bare_cox_on_a_terminal_execs_coxtop_and_a_flag_keeps_the_session(tmp_path: Path, monkeypatch, capsys):
    calls = _terminal_with(monkeypatch, coxtop=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\nskills_roots: []\n")
    with pytest.raises(_Execd):
        main([])
    assert calls == [("coxtop", ["coxtop"])]
    capsys.readouterr()
    assert main(["--profile", str(profile), "--print-argv"]) == 0
    assert calls == [("coxtop", ["coxtop"])]
    assert capsys.readouterr().out.splitlines()[-2:] == ["['claude']", str(tmp_path)]


def test_cox_home_execs_coxtop_and_keeps_curses_for_a_profile_or_without_coxtop(tmp_path: Path, monkeypatch):
    curses = []
    monkeypatch.setattr("agent_tools.home_screen.main", lambda *a, **k: curses.append(a) or 0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {workspace}\nskills_roots: []\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile))
    calls = _terminal_with(monkeypatch, coxtop=True)
    with pytest.raises(_Execd):
        main(["home"])
    assert calls == [("coxtop", ["coxtop"])] and curses == []
    main(["home", "--profile", str(profile)])
    assert calls == [("coxtop", ["coxtop"])] and len(curses) == 1
    _terminal_with(monkeypatch, coxtop=False)
    main(["home"])
    assert len(curses) == 2

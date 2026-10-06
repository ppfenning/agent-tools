from pathlib import Path

import pytest

from agent_tools.cli import main, should_exec_coxtop


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


def _stub_launch(monkeypatch, result: bool) -> list:
    """`coxtop_launch.launch` is replaced and records each call."""
    calls = []
    monkeypatch.setattr("agent_tools.coxtop_launch.launch", lambda *a, **k: calls.append((a, k)) or result)
    return calls


def _fake_context(monkeypatch, tmp_path: Path) -> None:
    """The route context gather returns a fixed profile and empty workspace."""
    profile = {"team": "fixed-team", "workspace_dir": str(tmp_path)}
    groups = {"queued": [], "decomposed": [], "landed": []}
    monkeypatch.setattr("agent_tools.cli._lake_provider", lambda a: ({}, None))
    monkeypatch.setattr("agent_tools.cli._gather_context", lambda path, mode: (profile, None, groups, [], [], [], None))
    monkeypatch.setattr("agent_tools.cli._usage_assessment", lambda *a, **k: type("U", (), {"reason": "usage ok"})())


def test_cox_home_returns_without_printing_when_the_launcher_took_the_terminal(monkeypatch, capsys):
    calls = _stub_launch(monkeypatch, True)
    assert main(["home"]) == 0
    assert len(calls) == 1
    assert capsys.readouterr().out == ""


def test_cox_home_prints_the_route_context_when_the_launcher_falls_back(tmp_path: Path, monkeypatch, capsys):
    calls = _stub_launch(monkeypatch, False)
    _fake_context(monkeypatch, tmp_path)
    assert main(["home"]) == 0
    assert len(calls) == 1
    assert "intake: 0 queued, 0 decomposed, 0 landed" in capsys.readouterr().out.splitlines()


def test_cox_home_with_a_profile_never_calls_the_launcher(tmp_path: Path, monkeypatch, capsys):
    calls = _stub_launch(monkeypatch, True)
    _fake_context(monkeypatch, tmp_path)
    assert main(["home", "--profile", str(tmp_path / "profile.yaml")]) == 0
    assert calls == []
    assert "intake: 0 queued, 0 decomposed, 0 landed" in capsys.readouterr().out.splitlines()

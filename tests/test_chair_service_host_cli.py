from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import cli


class FakeRun:
    """Stands in for `subprocess.run`. `git ... remote get-url origin` always succeeds; the first
    ssh/sudo argv (an install step) fails when `fail_first` is set, and every call is recorded."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail_first = fail_first
        self._step_calls = 0

    def __call__(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git@example.com:x/y.git\n", stderr="")
        self._step_calls += 1
        if self.fail_first and self._step_calls == 1:
            return subprocess.CompletedProcess(argv, 1, stdout="boom\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def step_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] in ("ssh", "sudo")]


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "profile.yaml").write_text(f"workspace_dir: {tmp_path}/ws\n")
    monkeypatch.setattr(
        cli.run_store, "hosts", lambda _runs_dir: [{"name": "jarvis", "ssh": "jarvis.example.com"}],
    )
    return tmp_path


def _install(home: Path, fake: FakeRun, *flags: str) -> int:
    return cli.main(["chair", "service", "--install", "--host", "jarvis", "--profile", str(home / "profile.yaml"), *flags])


def test_install_host_without_apply_prints_the_steps_and_runs_none_of_them(home: Path, monkeypatch, capsys) -> None:
    fake = FakeRun()
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert _install(home, fake) == 0
    assert fake.step_calls() == []
    out = capsys.readouterr().out
    assert "ssh jarvis.example.com" in out
    assert "# run by hand" in out


def test_install_host_apply_stops_at_the_first_failing_step(home: Path, monkeypatch, capsys) -> None:
    fake = FakeRun(fail_first=True)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    rc = _install(home, fake, "--apply")
    assert rc == 1
    assert len(fake.step_calls()) == 1
    assert "boom" in capsys.readouterr().out


def test_install_unknown_host_exits_2_with_one_line(home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.run_store, "hosts", lambda _runs_dir: [])
    fake = FakeRun()
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert _install(home, fake) == 2
    assert capsys.readouterr().out == "unknown host: jarvis\n"
    assert fake.calls == []

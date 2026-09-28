from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import cli


class FakeRun:
    """Stands in for `subprocess.run`. Each ssh command below gets its own canned result; any other
    argv (there is none in these tests) would raise KeyError, which fails the test loudly."""

    def __init__(self, results: dict[str, subprocess.CompletedProcess]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return self.results[argv[-1]]


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "profile.yaml").write_text(f"workspace_dir: {tmp_path}/ws\n")
    monkeypatch.setattr(
        cli.run_store, "hosts", lambda _runs_dir: [{"name": "jarvis", "ssh": "jarvis.example.com"}],
    )
    return tmp_path


def _status(home: Path, *flags: str) -> int:
    return cli.main(["chair", "service", "--status", "--host", "jarvis", "--profile", str(home / "profile.yaml"), *flags])


def test_all_three_ssh_calls_succeed_and_the_line_carries_all_three_values(home: Path, monkeypatch, capsys) -> None:
    fake = FakeRun({
        "systemctl --user is-active coxswain-chair.service": subprocess.CompletedProcess([], 0, stdout="active\n", stderr=""),
        "cox runs top --once": subprocess.CompletedProcess([], 0, stdout="3 lanes running\nmore\n", stderr=""),
        "tail -1 repos/workspace/runs/chair-loop.log": subprocess.CompletedProcess([], 0, stdout="2026-09-28T00:00Z tick\n", stderr=""),
    })
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert _status(home) == 0
    assert capsys.readouterr().out == "jarvis: active; 3 lanes running; 2026-09-28T00:00Z tick\n"


def test_the_unit_ssh_call_fails_and_the_line_says_no_unit(home: Path, monkeypatch, capsys) -> None:
    fake = FakeRun({
        "systemctl --user is-active coxswain-chair.service": subprocess.CompletedProcess([], 3, stdout="inactive\n", stderr=""),
        "cox runs top --once": subprocess.CompletedProcess([], 0, stdout="3 lanes running\n", stderr=""),
        "tail -1 repos/workspace/runs/chair-loop.log": subprocess.CompletedProcess([], 0, stdout="2026-09-28T00:00Z tick\n", stderr=""),
    })
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert _status(home) == 0
    assert capsys.readouterr().out == "jarvis: no unit; 3 lanes running; 2026-09-28T00:00Z tick\n"


def test_an_unknown_host_exits_2(home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.run_store, "hosts", lambda _runs_dir: [])
    fake = FakeRun({})
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert _status(home) == 2
    assert capsys.readouterr().out == "unknown host: jarvis\n"
    assert fake.calls == []

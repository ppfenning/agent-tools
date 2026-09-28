from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import cli


@pytest.fixture(autouse=True)
def _usable_provider(monkeypatch) -> None:
    """The suite's profile env var names no file, which `_lake_provider` reports as a problem; the proof runs in files mode."""
    monkeypatch.setattr(cli, "_lake_provider", lambda _a: ({}, None))


def _no_ccusage(*_a, **_k) -> None:
    raise FileNotFoundError("npx")


def _item(phase_dir: Path, name: str, state: str) -> None:
    (phase_dir / f"{name}.md").write_text(f"---\nid: {name}\nstate: {state}\n---\nbody\n", encoding="utf-8")


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    """(runs_dir, profile_path) over a workspace holding a started initiative, an approved and a quarantined item, one intake file and one source."""
    ws = tmp_path / "ws"
    phase = ws / "work" / "init" / "build"
    phase.mkdir(parents=True)
    (ws / "work" / "init" / "initiative.md").write_text("---\nid: init\nrepo: /r/x\n---\nbody\n", encoding="utf-8")
    _item(phase, "done1", "done")
    _item(phase, "appr1", "approved")
    (phase / "quar1.md").write_text(
        "---\nid: quar1\nstate: ready\nattempts:\n  - run: init-1\n    ts: 2020-01-01T00:00:00Z\n---\nbody\n", encoding="utf-8"
    )
    (ws / "intake").mkdir()
    (ws / "intake" / "idea.md").write_text("an idea\n", encoding="utf-8")
    runs_dir = ws / "runs"
    runs_dir.mkdir()
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f'workspace_dir: {ws}\nsources: {{"github": {{"repos": ["a/b"]}}}}\n', encoding="utf-8")
    return runs_dir, profile_path


def test_the_real_deps_over_a_populated_workspace_name_no_unwired_source(tmp_path) -> None:
    runs_dir, profile_path = _workspace(tmp_path)
    deps = cli._chair_run_deps(runs_dir, {}, "chair", 1, "h", True, print, profile_path, "files")
    facts = deps.facts_deps
    assert cli._chair_unwired_sources(deps) == []
    assert [r["id"] for r in facts.approved()] == ["appr1"]
    assert [r["task"] for r in facts.quarantined()] == ["quar1"]
    assert facts.intake() == ["intake/idea.md"]
    assert facts.sources_configured() is True
    assert facts.stranded() == []
    assert [row["started"] for row in facts.docket()["initiatives"]] == [True]


def test_chair_run_once_dry_run_prints_one_line_and_takes_no_lease(monkeypatch, tmp_path, capsys) -> None:
    runs_dir, profile_path = _workspace(tmp_path)
    monkeypatch.setattr(cli.chair, "renew_lease", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("lease renewed")))
    monkeypatch.setattr(cli.chair, "release_lease", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("lease released")))
    real_gather = cli.usage_window.gather  # its default `run` is `subprocess.run`, which would launch npx and may reach the network
    monkeypatch.setattr(cli.usage_window, "gather", lambda runs, now, **kw: real_gather(runs, now, run=_no_ccusage, **kw))
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile_path))
    args = cli.build_parser().parse_args(["chair", "run", "--once", "--dry-run"])
    assert args.fn(args) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    assert list(runs_dir.iterdir()) == []

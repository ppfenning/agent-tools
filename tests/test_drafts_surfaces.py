from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from agent_tools import chair_run, cli, draft_apply, draft_list, route
from agent_tools.chair_plan import plan_tick

PROPOSED_AT = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
FREE = {"holder": "", "host": "", "epoch": 0, "released": True, "stale": False}


@pytest.fixture(autouse=True)
def _usable_provider(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_lake_provider", lambda _a: ({}, None))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _draft(ws: Path, name: str = "idea", proposer: str = "steward") -> Path:
    """A draft initiative with one `todo` task, as the steward files it."""
    root = ws / "work" / name
    _write(root / "initiative.md", f"---\nid: {name}\ndraft: true\nproposed_by: {proposer}\nproposed_at: {PROPOSED_AT}\n---\n\nBody\n")
    _write(root / "1-build" / "task.md", "---\nid: task\nstate: todo\nneeds: []\n---\n\nBody\n")
    return root.parent


def _tick(ws: Path) -> tuple[str, list[str]]:
    """One real dry-run tick over `ws`: the echoed status line, and the action kinds the real planner chose."""
    (ws / "runs").mkdir(exist_ok=True)
    lines: list[str] = []
    kinds: list[str] = []

    def plan(facts, _now):
        planned = plan_tick(facts)
        kinds.extend(a["kind"] for a in planned)
        return planned

    real = cli._chair_run_deps(ws / "runs", {}, "chair", 1, "h", True, lines.append, ws / "profile.yaml", "files")
    deps = dataclasses.replace(real, plan=plan, facts_deps=dataclasses.replace(real.facts_deps, lease=lambda: FREE))
    chair_run.run(True, 60, True, deps)
    return lines[-1], kinds  # the timing line comes first; the status line is the last one echoed


def test_a_draft_shows_in_the_status_line_and_the_chair_plans_no_launch_for_it(tmp_path) -> None:
    _draft(tmp_path)
    line, kinds = _tick(tmp_path)
    assert "drafts 1" in line
    assert "launch_epic" not in kinds


def test_approving_the_draft_makes_the_next_tick_launch_it_and_drops_the_drafts_count(tmp_path) -> None:
    work = _draft(tmp_path)
    store = draft_apply.Store(mode="files", rows=lambda _i: [], set_state=lambda *a, **k: None)
    assert draft_apply.approve(work, "idea", None, "me", store) == 0
    line, kinds = _tick(tmp_path)
    assert "drafts" not in line
    assert kinds == ["launch_epic"]


def test_no_draft_means_no_drafts_segment(tmp_path) -> None:
    line, _kinds = _tick(tmp_path)
    assert "drafts" not in line


def test_render_drafts_lists_id_proposer_and_age_and_is_empty_for_none() -> None:
    rows = [draft_list.DraftRow("idea", "steward", 172800), draft_list.DraftRow("other", "gate", None)]
    assert route.render_drafts(rows, draft_list.format_age) == "drafts:\n  idea  steward  2d\n  other  gate  ?"
    assert route.render_drafts([], draft_list.format_age) == ""


def _profile(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n", encoding="utf-8")
    return profile


def test_route_status_lists_the_draft_with_its_proposer_and_age(tmp_path, capsys) -> None:
    profile = _profile(tmp_path)
    _draft(tmp_path / "ws")
    assert cli.main(["route", "status", "--profile", str(profile)]) == 0
    assert "drafts:\n  idea  steward  2d" in capsys.readouterr().out


def test_route_status_with_no_drafts_prints_what_it_printed_before(tmp_path, capsys) -> None:
    profile = _profile(tmp_path)
    assert cli.main(["route", "status", "--profile", str(profile)]) == 0
    out = capsys.readouterr().out
    assert "drafts" not in out
    assert out == route.render_status([], None, [], gate_level=cli._resolved_gate_level(tmp_path / "ws" / "runs")) + "\n"

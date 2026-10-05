from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import chair_read_docket, pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    gather_facts,
    read_initiative_repos,
    read_ticket_items,
    running_initiatives,
    steer_streaks_from_actions,
    ticket_surfaces,
)
from agent_tools.chair_steer import steer_check

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)


def _item(initiative: str, task: str, state: str, surfaces: list[str]) -> dict:
    return {"initiative": initiative, "id": task, "state": state, "surfaces": surfaces}


def test_ready_surfaces_are_the_sorted_union_of_the_ready_tasks():
    items = [_item("x", "a", "ready", ["a.rs"]), _item("x", "b", "ready", ["b.rs", "a.rs"]), _item("x", "c", "ready", [])]
    assert ticket_surfaces(items, {"x": {"a", "b", "c"}}) == {"x": ["a.rs", "b.rs"]}


def test_a_task_outside_the_ready_set_adds_no_surface():
    items = [_item("x", "a", "ready", ["a.rs"]), _item("x", "d", "done", ["d.rs"])]
    assert ticket_surfaces(items, {"x": {"a"}}) == {"x": ["a.rs"]}


def test_an_approved_tasks_surfaces_count_for_a_running_initiative_and_a_done_tasks_do_not():
    items = [_item("x", "a", "approved", ["a.rs"]), _item("x", "d", "done", ["d.rs"]), _item("y", "b", "ready", ["b.rs"])]
    assert running_initiatives({"x"}, items, {"x": "/r"}) == [{"id": "x", "repo": "/r", "surfaces": ["a.rs"]}]


def test_two_steer_clears_then_a_launch_of_the_initiative_leave_no_key():
    rows = [
        {"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "3", "kind": "launch_epic", "initiative": "x"},
    ]
    assert steer_streaks_from_actions(rows) == {}


def test_two_steer_clears_and_no_launch_count_two():
    rows = [
        {"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"},
        {"ts": "2", "kind": "steer_clear", "initiative": "x", "other": "y"},
    ]
    assert steer_streaks_from_actions(rows) == {"x|y": 2}


def test_a_store_row_carries_its_action_in_action_json_and_a_launch_of_another_initiative_keeps_the_count():
    rows = [
        {"ts": "2", "kind": "launch_epic", "action_json": '{"kind": "launch_epic", "initiative": "z"}'},
        {"ts": "1", "kind": "steer_clear", "action_json": '{"kind": "steer_clear", "initiative": "x", "other": "y"}'},
    ]
    assert steer_streaks_from_actions(rows) == {"x|y": 1}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def test_a_work_tree_with_two_initiatives_sharing_a_path_gives_steer_check_a_non_empty_overlap(tmp_path):
    _write(tmp_path / "work/x/initiative.md", "---\nid: x\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/y/initiative.md", "---\nid: y\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/x/p1/a.md", "---\nid: a\nstate: ready\nsurfaces: [src/a.rs, src/b.rs]\n---\n\nbody\n")
    _write(tmp_path / "work/y/p1/b.md", "---\nid: b\nstate: approved\nsurfaces:\n  - src/b.rs\n---\n\nbody\n")
    _write(tmp_path / "work/y/p1/c.md", "---\nid: c\nstate: ready\nsurfaces: [src/c.rs]\n---\n\nbody\n")
    assert read_initiative_repos(tmp_path) == {"x": "/r", "y": "/r"}
    assert len(read_ticket_items(tmp_path, "files")) == 3
    deps = FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: chair_read_docket.read_docket(tmp_path, "files", 2),
        approved=lambda: [],
        quarantined=lambda: [],
        stranded=lambda: [],
        attempts=lambda: [],
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: ["y"],
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        tickets=lambda: read_ticket_items(tmp_path, "files"),
        repos=lambda: read_initiative_repos(tmp_path),
        actions=lambda: [{"ts": "1", "kind": "steer_clear", "initiative": "x", "other": "y"}],
    )
    facts = gather_facts(deps, NOW)
    (entry,) = facts["initiatives"]
    assert (entry["id"], entry["repo"], entry["ready_surfaces"]) == ("x", "/r", ["src/a.rs", "src/b.rs"])
    assert facts["running"] == [{"id": "y", "repo": "/r", "surfaces": ["src/b.rs", "src/c.rs"]}]
    assert facts["steer_streaks"] == {"x|y": 1}
    action = steer_check(entry["id"], entry["repo"], entry["ready_surfaces"], facts["running"], facts["steer_streaks"])
    assert action == {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/b.rs"]}

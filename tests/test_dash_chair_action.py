from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_tools.chair_read_record import ACTION_LOG, recorder
from agent_tools.dash_chair_action import current_action, read_current_action

LAND = {"kind": "land", "initiative": "alpha", "phase": "p1"}
EXPECTED = {"kind": "land", "target": "alpha/p1", "since": "2026-10-04T10:00:00+00:00"}


def _fake_perform(runs_dir: Path, probe: Callable[[], None]) -> None:
    """Stages the start-then-close shape the reader expects: an open line, the work, a closing line."""
    record = recorder(runs_dir, lambda: 1, lambda: "2026-10-04T10:00:00+00:00")
    record(LAND)
    probe()
    record({**LAND, "status": "landed"})


def test_reader_sees_the_action_while_it_runs_and_none_after(tmp_path: Path) -> None:
    seen: list[dict[str, str] | None] = []
    _fake_perform(tmp_path, lambda: seen.append(read_current_action(tmp_path)))
    assert seen == [EXPECTED]
    assert read_current_action(tmp_path) is None


def test_missing_log_is_none(tmp_path: Path) -> None:
    assert read_current_action(tmp_path) is None


def test_blank_and_malformed_lines_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ACTION_LOG).write_text(
        '\nnot json\n[1]\n{"kind": "land", "initiative": "alpha", "phase": "p1", "ts": "t"}\n', encoding="utf-8"
    )
    assert read_current_action(tmp_path) == {"kind": "land", "target": "alpha/p1", "since": "t"}


def test_empty_records_are_none() -> None:
    assert current_action([]) is None


def test_closed_record_is_none() -> None:
    assert current_action([{**LAND, "status": "landed", "ts": "t"}]) is None


def test_open_record_without_a_phase_is_none() -> None:
    assert current_action([{"kind": "launch_epic", "initiative": "alpha", "ts": "t"}]) is None


def test_close_of_another_action_leaves_the_open_one() -> None:
    other = {"kind": "land", "initiative": "alpha", "phase": "p2", "status": "landed", "ts": "u"}
    assert current_action([{**LAND, "ts": "t"}, other]) == {"kind": "land", "target": "alpha/p1", "since": "t"}

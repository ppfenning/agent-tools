import os

import pytest

from agent_tools import chair_decompose_runs as mod
from agent_tools.chair_decompose_streak import DecomposeRun

OLD = "chair: plan approved but not executed: old"
NEW = "chair: plan approved but not executed: new"


@pytest.fixture
def parses(tmp_path, monkeypatch):
    mod._REFUSALS.clear()
    calls: list[str] = []
    real = mod.refusal_line

    def counting(log: str) -> str | None:
        if log:  # parse_run itself scans the empty string when the edge hands it no log
            calls.append(log)
        return real(log)

    monkeypatch.setattr(mod, "refusal_line", counting)
    monkeypatch.setattr(mod, "exit_rows", lambda runs_dir: [{"run_id": "idea-1", "launched_at": "1"}])
    monkeypatch.setattr(mod, "row_exited", lambda row: True)
    monkeypatch.setattr(mod, "_task_items", lambda runs_dir, initiative: 0)
    yield calls
    mod._REFUSALS.clear()


def test_unchanged_log_parses_once_and_a_new_mtime_parses_again(tmp_path, parses):
    log = tmp_path / "idea-1.log"
    log.write_text(f"{OLD}\n")
    os.utime(log, ns=(1_000_000_000, 1_000_000_000))

    first = mod.read_decompose_runs(tmp_path, "idea")
    second = mod.read_decompose_runs(tmp_path, "idea")
    assert first == second == [DecomposeRun("idea-1", False, OLD)]
    assert parses == [f"{OLD}\n"]

    log.write_text(f"{NEW}\n")
    os.utime(log, ns=(2_000_000_000, 2_000_000_000))
    assert mod.read_decompose_runs(tmp_path, "idea") == [DecomposeRun("idea-1", False, NEW)]
    assert parses == [f"{OLD}\n", f"{NEW}\n"]


def test_missing_log_reads_as_no_refusal_and_is_not_parsed(tmp_path, parses):
    assert mod.read_decompose_runs(tmp_path, "idea") == [DecomposeRun("idea-1", False, None)]
    assert parses == []

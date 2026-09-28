import json

from agent_tools import chair_facts, cli


def _write_task(runs_dir, run_id, phase, task, **fields):
    d = runs_dir / run_id / "tasks" / phase
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{task}.json"
    path.write_text(json.dumps({"run": run_id, "task": task, "phase": phase, **fields}))
    return path


def _write_manifest(runs_dir, run_id, phase):
    (runs_dir / f"{run_id}:{phase}.json").write_text(json.dumps({}))


def test_a_ticket_recorded_only_in_an_earlier_run_falls_back_to_that_run(tmp_path, monkeypatch):
    monkeypatch.setattr(chair_facts, "run_initiative", lambda run: "zephyr")
    runs_dir = tmp_path / "runs"
    a_path = _write_task(runs_dir, "zephyr-2", "seams", "A")
    _write_task(runs_dir, "zephyr-4", "seams", "B")
    _write_manifest(runs_dir, "zephyr-4", "seams")

    _phase_record, task_records, task_paths, _searched = cli._land_phase_record(runs_dir, "zephyr-4", "seams")

    assert {r["task"] for r in task_records} == {"A", "B"}
    assert task_paths["A"] == str(a_path)


def test_a_ticket_recorded_in_both_runs_takes_the_current_run(tmp_path, monkeypatch):
    monkeypatch.setattr(chair_facts, "run_initiative", lambda run: "zephyr")
    runs_dir = tmp_path / "runs"
    _write_task(runs_dir, "zephyr-2", "seams", "C", marker="old")
    c_path = _write_task(runs_dir, "zephyr-4", "seams", "C", marker="new")
    _write_manifest(runs_dir, "zephyr-4", "seams")

    _phase_record, task_records, task_paths, _searched = cli._land_phase_record(runs_dir, "zephyr-4", "seams")

    c_record = next(r for r in task_records if r["task"] == "C")
    assert c_record["marker"] == "new"
    assert task_paths["C"] == str(c_path)


def test_a_ticket_recorded_nowhere_is_absent_from_the_result(tmp_path, monkeypatch):
    monkeypatch.setattr(chair_facts, "run_initiative", lambda run: "zephyr")
    runs_dir = tmp_path / "runs"
    _write_task(runs_dir, "zephyr-2", "seams", "A")
    _write_manifest(runs_dir, "zephyr-4", "seams")

    _phase_record, task_records, task_paths, _searched = cli._land_phase_record(runs_dir, "zephyr-4", "seams")

    assert "D" not in {r["task"] for r in task_records}
    assert "D" not in task_paths

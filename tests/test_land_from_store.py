import json
import subprocess as sp
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools import cli, land, run_store, store_cli

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_RECORD = {
    "run": "epic-x-5", "task": "seams-task", "phase": "seams", "initiative": "x",
    "proposals": [{"kind": "draft_pr_create", "title": "Add seams"}],
    "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
    "change_facts": {"fix_loop_attempts": 1, "checks": "pytest -q"}, "build": {"summary": "Added the seams module."},
}
_APPROVED = "---\nid: seams-task\nstate: approved\n---\nBody.\n"
_HARNESS = Path("/h/.venv/bin/python")


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(cli.tempfile, "tempdir", str(tmp_path / "tmp"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for key, value in (("user.email", "t@e"), ("user.name", "t")):
        sp.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "f").write_text("x")
    sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "checkout", "-qb", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    (root / "f").write_text("y")
    sp.run(["git", "-C", str(root), "commit", "-aqm", "Add seams module"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, env=_ENV)
    return root


@pytest.fixture
def store(monkeypatch):
    """Fakes for the store edge. `calls` is the ordered log; the outcomes are attributes a test may replace.
    `states` is read in turn, the last one repeating."""
    fake = SimpleNamespace(
        calls=[], states=["approved"], acquire=store_cli.LeaseGranted(7, "me"), renew=store_cli.LeaseGranted(7, "me"),
        # `cli._runs_land`'s own repo-scoped lease (ticket one-land-per-repository-at-a-time) calls the same
        # store_cli.lease_acquire/lease_release this fixture fakes; its lease name is `land:<repo path>`, always
        # an absolute path, so `land:/` distinguishes it from the task/phase names below ("land:seams-task",
        # "land:phase:x/seams") without needing the `repo` fixture wired into this one.
        repo_acquire=store_cli.LeaseGranted(1, "repo-holder"), repo_release=store_cli.LeaseReleased(),
        item=None, seen_at_release=None, set_result=store_cli.StateSet({"state": "done"}), real_set_state=store_cli.set_state,
    )

    def task_state_of(runs_dir, initiative, task):
        fake.calls.append(("task_state", initiative, task))
        return fake.states.pop(0) if len(fake.states) > 1 else fake.states[0]

    def acquire(runs_dir, name, holder, ttl):
        fake.calls.append(("acquire", name))
        return fake.repo_acquire if name.startswith("land:/") else fake.acquire

    def renew(runs_dir, name, holder, epoch, ttl):
        fake.calls.append(("renew", name, epoch))
        return fake.renew

    def release(runs_dir, name, holder, epoch):
        fake.calls.append(("release", name, epoch))
        fake.seen_at_release = fake.item.read_text(encoding="utf-8") if fake.item.exists() else None
        return fake.repo_release if name.startswith("land:/") else store_cli.LeaseReleased()

    def set_state(runs_dir, initiative, task, state, by, expected=None):
        fake.calls.append(("set_state", initiative, task, state, expected))
        return fake.set_result

    monkeypatch.setattr(run_store, "task_state_of", task_state_of)
    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    monkeypatch.setattr(store_cli, "lease_renew", renew)
    monkeypatch.setattr(store_cli, "lease_release", release)
    monkeypatch.setattr(store_cli, "set_state", set_state)
    monkeypatch.setattr(cli, "_mirror_landed", lambda step: None)
    return fake


def _land(repo, tmp_path, monkeypatch, store, *, work_state="store", fail_at=None, raise_at=None, extra=(), item_text=_APPROVED,
          phase=False, filed=True):
    """Land `seams-task` with every step stubbed except mark_done. Returns the exit code and the step kinds run.

    `item_text` None leaves the work item file missing. `phase` sets up a phase land, whose items are already done.
    `filed` False leaves the task record file unwritten."""
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"
    task_dir.mkdir(parents=True)
    if filed:
        (task_dir / "seams-task.json").write_text(json.dumps(_RECORD), encoding="utf-8")
    if phase:
        (tmp_path / "runs/epic-x-5:seams.json").write_text(json.dumps({"phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
        item_text = _APPROVED.replace("approved", "done")
    store.item = tmp_path / "work/x/seams/seams-task.md"
    store.item.parent.mkdir(parents=True, exist_ok=True)
    if item_text is not None:
        store.item.write_text(item_text, encoding="utf-8")
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"work_state: {work_state}\n" if work_state else "", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"provider_profile: {provider}\n", encoding="utf-8")
    real, store.ran = cli._execute_land_step, []

    def step(_repo, step, _forge=None):
        store.ran.append(step["kind"])
        if step["kind"] == raise_at:
            raise RuntimeError("step blew up")
        if step["kind"] == "mark_done":
            return real(_repo, step, _forge)
        return step["kind"] != fail_at, "ok"

    monkeypatch.setattr(cli, "_execute_land_step", step)
    argv = ["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--gate", "full", "--profile", str(profile),
            "--runs-dir", str(tmp_path / "runs"), *extra]
    return cli.main(argv), store.ran


def test_a_store_that_says_done_stops_the_land_before_any_step(repo, tmp_path, monkeypatch, store, capsys):
    store.states = ["done"]
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    captured = capsys.readouterr()
    assert (rc, ran) == (2, [])
    assert "task is already done; another machine landed it" in captured.out
    assert "land: state from store" in captured.err
    assert [c[0] for c in store.calls] == ["task_state"]


def test_the_approved_check_is_read_again_under_the_lease(repo, tmp_path, monkeypatch, store, capsys):
    store.states = ["approved", "done"]
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran) == (2, [])
    assert "another machine landed it" in capsys.readouterr().out
    # The repo-scoped lease (ticket one-land-per-repository-at-a-time) wraps the task-scoped one: an extra
    # acquire before it and an extra release after, both named `land:<repo path>`.
    assert [c[0] for c in store.calls] == ["task_state", "acquire", "acquire", "task_state", "release", "release"]


def test_a_store_with_no_row_falls_back_to_the_file_and_says_why(repo, tmp_path, monkeypatch, store, capsys):
    store.states = [None]
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran[-1]) == (0, "mark_done")
    assert "land: state from file (the store returned no row for x/seams-task: none recorded, or the read failed)" in capsys.readouterr().err


def test_a_refused_lease_stops_the_land_naming_the_holder_before_any_step(repo, tmp_path, monkeypatch, store, capsys):
    store.acquire = store_cli.LeaseRefused(4, "other@host:99")
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran) == (2, [])
    assert "seams-task is being landed by other@host:99 (lease epoch 4)" in capsys.readouterr().out
    # The repo-scoped lease acquires and releases around the refused task-scoped acquire.
    assert [c[0] for c in store.calls] == ["task_state", "acquire", "acquire", "release"]
    rows = [json.loads(line) for line in (tmp_path / "runs/land.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["task"], r["steps_reached"], r["exit"]) for r in rows] == [("seams-task", [], 2)]


def test_an_unavailable_lease_stops_the_land_before_any_step(repo, tmp_path, monkeypatch, store, capsys):
    store.acquire = store_cli.NotAvailable()
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran) == (2, [])
    assert "could not take the land lease for seams-task: no harness" in capsys.readouterr().out


def test_a_lease_lost_before_the_merge_stops_the_land_before_the_merge(repo, tmp_path, monkeypatch, store, capsys):
    store.renew = store_cli.LeaseRefused(9, "other@host:1")
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert rc == 2 and "merge" not in ran
    assert "refusing merge, the land lease for seams-task could not be renewed: held by other@host:1 at epoch 9" in capsys.readouterr().out
    assert store.calls[-3:] == [("renew", "land:seams-task", 7), ("release", "land:seams-task", 7), ("release", f"land:{repo}", 1)]


def test_the_lease_is_released_after_a_merge_step_fails(repo, tmp_path, monkeypatch, store):
    rc, ran = _land(repo, tmp_path, monkeypatch, store, fail_at="merge")
    assert (rc, ran[-1]) == (1, "merge")
    assert store.calls[-2:] == [("release", "land:seams-task", 7), ("release", f"land:{repo}", 1)]
    assert store.item.read_text(encoding="utf-8") == _APPROVED


def test_the_lease_is_released_when_a_step_raises(repo, tmp_path, monkeypatch, store):
    with pytest.raises(RuntimeError, match="step blew up"):
        _land(repo, tmp_path, monkeypatch, store, raise_at="merge")
    assert store.ran[-1] == "merge"
    assert store.calls[-2:] == [("release", "land:seams-task", 7), ("release", f"land:{repo}", 1)]


def test_the_lease_is_held_until_mark_done_has_finished_then_released(repo, tmp_path, monkeypatch, store):
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran[-1]) == (0, "mark_done")
    assert [c[0] for c in store.calls] == ["task_state", "acquire", "acquire", "task_state", "renew", "renew", "set_state",
                                            "release", "release"]
    assert store.calls[6] == ("set_state", "x", "seams-task", "done", "approved")
    assert "state: done" in store.seen_at_release


def test_exit_3_at_mark_done_stops_with_the_landed_elsewhere_reason(repo, tmp_path, monkeypatch, store, capsys):
    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)  # a chair session exports it, and `--by` reads it
    real_run, harness = sp.run, []
    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)

    def fake_run(argv, **kwargs):
        if argv[0] != str(_HARNESS):
            return real_run(argv, **kwargs)
        harness.append(argv)
        return sp.CompletedProcess(argv, 3, stdout='{"state": "done"}', stderr="")

    monkeypatch.delenv("COX_SESSION_LABEL", raising=False)  # `--by` falls back to the ambient label, so pin it to "unlabeled"
    monkeypatch.setattr(store_cli, "set_state", store.real_set_state)
    monkeypatch.setattr(store_cli, "_harness_python", lambda: _HARNESS)
    monkeypatch.setattr(store_cli.subprocess, "run", fake_run)
    rc, ran = _land(repo, tmp_path, monkeypatch, store)
    assert (rc, ran[-1]) == (1, "mark_done")
    assert "another machine landed this task; the store state is no longer approved (now 'done')" in capsys.readouterr().out
    assert [argv[3:] for argv in harness] == [["set-state", "x", "seams-task", "done", "--by", "unlabeled", "--expect", "approved",
                                               "--store-url", store_cli._store_url(tmp_path / "runs")]]
    assert store.item.read_text(encoding="utf-8") == _APPROVED
    assert store.calls[-1][0] == "release"


@pytest.mark.parametrize("item_text", [None, "---\nid: seams-task\nstate: blocked\n---\nBody.\n", "---\nid: seams-task\nstate: done\n---\nBody.\n"])
def test_a_store_that_says_approved_is_set_done_whatever_the_local_file_holds(repo, tmp_path, monkeypatch, store, item_text):
    rc, ran = _land(repo, tmp_path, monkeypatch, store, item_text=item_text)
    assert (rc, ran[-1]) == (0, "mark_done")
    assert store.calls[6] == ("set_state", "x", "seams-task", "done", "approved")
    assert (store.item.read_text(encoding="utf-8") if store.item.exists() else None) == item_text


def test_a_phase_land_under_store_takes_one_phase_lease_and_reads_task_state_from_the_store(repo, tmp_path, monkeypatch, store, capsys):
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert (rc, ran[-1]) == (0, "mark_done")
    # `_phase_items` reads the phase's one item's status from the store before any lease is taken, and now
    # reads "approved" rather than the file's "done", so `mark_done` sets it done through the store too; the
    # repo-scoped lease (acquired first, released last) wraps the one phase-scoped lease this test names.
    assert store.calls == [("task_state", "x", "seams-task"), ("acquire", f"land:{repo}"), ("acquire", "land:phase:x/seams"),
                           ("renew", "land:phase:x/seams", 7), ("renew", "land:phase:x/seams", 7),
                           ("set_state", "x", "seams-task", "done", "approved"),
                           ("release", "land:phase:x/seams", 7), ("release", f"land:{repo}", 1)]
    assert "no approved check" not in capsys.readouterr().out


def test_a_phase_lease_refused_stops_the_phase_land_before_any_step(repo, tmp_path, monkeypatch, store, capsys):
    store.acquire = store_cli.LeaseRefused(2, "other@host:5")
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert (rc, ran) == (2, [])
    assert "phase:x/seams is being landed by other@host:5 (lease epoch 2)" in capsys.readouterr().out


def test_files_mode_reads_no_task_state_and_takes_no_lease_and_sets_no_expectation(repo, tmp_path, monkeypatch, store, capsys):
    rc, ran = _land(repo, tmp_path, monkeypatch, store, work_state=None)
    assert (rc, ran[-1]) == (0, "mark_done")
    # No task-scoped lease under files mode; the repo-scoped lease still wraps the run regardless of mode.
    assert store.calls == [("acquire", f"land:{repo}"), ("set_state", "x", "seams-task", "done", None),
                           ("release", f"land:{repo}", 1)]  # the existing unconditional mirror, no --expect
    assert "state: done" in store.item.read_text(encoding="utf-8")
    assert "land: state from" not in capsys.readouterr().err


def test_backfill_writes_missing_task_record_files_from_the_store_and_they_plan(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "epic-x-5:seams.json").write_text(json.dumps({"phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
    monkeypatch.setattr(run_store, "run_task_ids", lambda runs_dir, run_id: ["a-task", "b-task"])
    monkeypatch.setattr(run_store, "task_record",
                         lambda runs_dir, run_id, phase, task: {"task": task, "phase": phase, "run": run_id})
    cli._backfill_phase_task_files(runs_dir, "epic-x-5", "seams")
    phase_dir = runs_dir / "epic-x-5" / "tasks" / "seams"
    assert sorted(p.name for p in phase_dir.glob("*.json")) == ["a-task.json", "b-task.json"]
    assert json.loads((phase_dir / "a-task.json").read_text(encoding="utf-8")) == {"task": "a-task", "phase": "seams", "run": "epic-x-5"}
    _, task_records, task_paths, _ = cli._land_phase_record(runs_dir, "epic-x-5", "seams")
    assert sorted(r["task"] for r in task_records) == ["a-task", "b-task"]
    assert set(task_paths) == {"a-task", "b-task"}


def test_a_store_mode_phase_land_with_no_record_files_backfills_both_from_the_store_and_lands(repo, tmp_path, monkeypatch, store):
    records = {task: {**_RECORD, "task": task} for task in ("other-task", "seams-task")}
    monkeypatch.setattr(run_store, "run_task_ids", lambda runs_dir, run_id: sorted(records))
    monkeypatch.setattr(run_store, "task_record", lambda runs_dir, run_id, phase, task: records[task] if phase == "seams" else None)
    (tmp_path / "work/x/seams").mkdir(parents=True)
    (tmp_path / "work/x/seams/other-task.md").write_text("---\nid: other-task\nstate: ready\n---\nBody.\n", encoding="utf-8")
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True, filed=False)
    assert (rc, ran[-1]) == (0, "mark_done")
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"
    assert sorted(p.name for p in task_dir.glob("*.json")) == ["other-task.json", "seams-task.json"]
    assert {k: v for k, v in json.loads((task_dir / "other-task.json").read_text(encoding="utf-8")).items() if k != "landed"} == records["other-task"]


def test_a_store_mode_phase_land_refuses_naming_a_ticket_with_no_work_items_row(repo, tmp_path, monkeypatch, store, capsys):
    store.states = [None]
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    assert rc == 2 and "mark_done" not in ran
    assert "refused: seams-task is None, not approved, done or dropped" in capsys.readouterr().out


def test_backfill_never_overwrites_an_existing_task_record_file(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    phase_dir = runs_dir / "epic-x-5" / "tasks" / "seams"
    phase_dir.mkdir(parents=True)
    local = '{"task": "a-task", "phase": "seams", "run": "epic-x-5", "local": true}'
    (phase_dir / "a-task.json").write_text(local, encoding="utf-8")
    monkeypatch.setattr(run_store, "run_task_ids", lambda runs_dir, run_id: ["a-task"])
    monkeypatch.setattr(run_store, "task_record",
                         lambda runs_dir, run_id, phase, task: {"task": task, "phase": phase, "run": run_id, "local": False})
    cli._backfill_phase_task_files(runs_dir, "epic-x-5", "seams")
    assert (phase_dir / "a-task.json").read_text(encoding="utf-8") == local


def test_backfill_takes_a_task_approved_in_an_earlier_run_tagged_with_that_run(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "epic-x-5:seams.json").write_text(json.dumps({"phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
    held = {"epic-x-5": ["b-task"], "epic-x-4": ["a-task", "b-task"], "epic-x-3": ["a-task"]}
    monkeypatch.setattr(run_store, "run_task_ids", lambda runs_dir, run_id: held.get(run_id, []))
    monkeypatch.setattr(run_store, "task_record", lambda runs_dir, run_id, phase, task: {"task": task, "phase": phase, "from": run_id})
    cli._backfill_phase_task_files(runs_dir, "epic-x-5", "seams")
    phase_dir = runs_dir / "epic-x-5" / "tasks" / "seams"
    read = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in phase_dir.glob("*.json")}
    # b-task comes from run 5 itself; a-task from the newest earlier run that holds it, tagged so the gather finds agents/epic-x-4/a-task.
    assert read == {"b-task": {"task": "b-task", "phase": "seams", "from": "epic-x-5", "run": "epic-x-5"},
                    "a-task": {"task": "a-task", "phase": "seams", "from": "epic-x-4", "run": "epic-x-4"}}
    _, task_records, _, _ = cli._land_phase_record(runs_dir, "epic-x-5", "seams")
    assert {r["task"]: r["run"] for r in task_records} == {"a-task": "epic-x-4", "b-task": "epic-x-5"}


def test_earlier_runs_are_newest_first_and_empty_for_an_unnumbered_id():
    assert cli._earlier_runs("epic-x-3") == ["epic-x-2", "epic-x-1"]
    assert cli._earlier_runs("epic-x-1") == []
    assert cli._earlier_runs("epic") == []


def test_phase_items_in_store_mode_reads_status_from_work_items_and_plans(tmp_path, monkeypatch):
    work_root = tmp_path / "work"
    (work_root / "x" / "seams").mkdir(parents=True)
    (work_root / "x" / "seams" / "a-task.md").write_text("---\nid: a-task\nstate: ready\n---\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(run_store, "task_state_of", lambda runs_dir, initiative, task: "approved")
    items, _ = cli._phase_items(work_root, "x", "seams", runs_dir=tmp_path / "runs", land_mode="store")
    assert items == [{"id": "a-task", "status": "approved", "file": str(work_root / "x" / "seams" / "a-task.md")}]
    assert land.phase_landable(items, {"a-task": _RECORD}) is None


def test_phase_items_in_store_mode_refuses_naming_the_task_with_no_work_items_row(tmp_path, monkeypatch):
    work_root = tmp_path / "work"
    (work_root / "x" / "seams").mkdir(parents=True)
    (work_root / "x" / "seams" / "a-task.md").write_text("---\nid: a-task\nstate: ready\n---\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(run_store, "task_state_of", lambda runs_dir, initiative, task: None)
    items, _ = cli._phase_items(work_root, "x", "seams", runs_dir=tmp_path / "runs", land_mode="store")
    assert items[0]["status"] is None
    assert land.phase_landable(items, {}) == "a-task is None, not approved, done or dropped"


def test_phase_items_in_files_mode_still_reads_status_from_the_ticket_file(tmp_path, monkeypatch):
    work_root = tmp_path / "work"
    (work_root / "x" / "seams").mkdir(parents=True)
    (work_root / "x" / "seams" / "a-task.md").write_text("---\nid: a-task\nstate: ready\n---\nBody.\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(run_store, "task_state_of", lambda *args: calls.append(args) or "approved")
    items, _ = cli._phase_items(work_root, "x", "seams")
    assert items == [{"id": "a-task", "status": "ready", "file": str(work_root / "x" / "seams" / "a-task.md")}]
    assert calls == []


def test_a_record_only_in_the_store_lands_and_mark_done_writes_the_file_from_it(repo, tmp_path, monkeypatch, store, capsys):
    stored = {k: v for k, v in _RECORD.items() if k not in ("run", "task", "phase")}
    monkeypatch.setattr(run_store, "task_record_phases", lambda runs_dir, run_id, task: ["seams"])
    monkeypatch.setattr(run_store, "task_record", lambda runs_dir, run_id, phase, task: stored)
    mirrored = []
    monkeypatch.setattr(cli, "_mirror_landed", mirrored.append)
    path = tmp_path / "runs/epic-x-5/tasks/seams/seams-task.json"
    rc, ran = _land(repo, tmp_path, monkeypatch, store, filed=False, extra=("--task", "seams-task"))
    assert (rc, ran[-1]) == (0, "mark_done")
    assert "land: record from store" in capsys.readouterr().err
    assert json.loads(path.read_text(encoding="utf-8")) == {**_RECORD, "landed": True}
    assert [(m["run"], m["phase"], m["task"]) for m in mirrored] == [("epic-x-5", "seams", "seams-task")]

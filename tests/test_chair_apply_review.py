from pathlib import Path

from agent_tools import chair_exec, store_cli
from agent_tools.chair_apply_review import DeleteBranch, MarkLanded, SetDone, apply_review, review_steps
from agent_tools.store_cli import Failed, Landed, StateSet

_URL = "https://github.com/o/r/pull/7"
_ACTION = {
    "kind": "review_landed", "epoch": 1, "initiative": "init", "phase": "build", "task_id": "a", "repo": "/r/x",
    "url": _URL, "merged_at": "2026-10-04T01:00:00Z", "run": "init-1", "host": "lane-a",
}


class _Fakes:
    def __init__(self, mark=None, set_=None, ssh="u@lane-a") -> None:
        self.calls: list[tuple] = []
        self.mark = Landed({}) if mark is None else mark
        self.set_ = StateSet({}) if set_ is None else set_
        self.ssh = ssh

    def apply(self, action: dict):
        return apply_review(
            action,
            set_state=lambda *a: self.calls.append(("set_state", *a)) or self.set_,
            mark_landed=lambda *a: self.calls.append(("mark_landed", *a)) or self.mark,
            delete_branches=lambda *a: self.calls.append(("delete", *a)) or ([a[2]], ""),
            ssh_for=lambda host: self.ssh,
            now=lambda: "2026-10-04T09:00:00Z",
        )


def test_review_landed_yields_the_steps_in_order_with_url_merge_time_and_branch() -> None:
    assert review_steps(_ACTION) == [
        SetDone("init", "a"),
        MarkLanded("init-1", "build", "a", _URL, "2026-10-04T01:00:00Z"),
        DeleteBranch("/r/x", "agents/init-1/a", ""),
        DeleteBranch("/r/x", "agents/init-1/a", "lane-a"),
    ]


def test_a_failed_mark_landed_reports_failed_and_deletes_no_branch() -> None:
    fakes = _Fakes(mark=Failed(1, "boom"))
    outcome = fakes.apply(_ACTION)
    assert (outcome.status, [c[0] for c in fakes.calls]) == ("failed", ["set_state", "mark_landed"])


def test_perform_dispatches_review_landed_and_records_it(monkeypatch) -> None:
    monkeypatch.setattr(store_cli, "set_state", lambda *a: StateSet({}))
    monkeypatch.setattr(store_cli, "mark_landed", lambda *a: Landed({}))
    recorded: list = []
    deps = chair_exec.Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([pattern], ""), acquire_lease=lambda *a, **k: "",
        record=recorded.append, run_id=lambda a: "", repo_for=lambda a: "", ssh_for=lambda host: "u@" + host, runs_dir=Path("."),
    )
    results = chair_exec.perform([_ACTION], deps, lambda: 1, False)
    assert ([r["status"] for r in results], [r["kind"] for r in recorded]) == (["done"], ["review_landed"])


def test_an_action_without_a_run_is_needs_chair_and_runs_nothing() -> None:
    fakes = _Fakes()
    outcome = fakes.apply({k: v for k, v in _ACTION.items() if k != "run"})
    assert (outcome.status, outcome.cause, fakes.calls) == ("needs_chair", "review_no_run", [])


def test_an_action_without_a_recorded_host_is_needs_chair_and_runs_nothing() -> None:
    fakes = _Fakes()
    outcome = fakes.apply({k: v for k, v in _ACTION.items() if k != "host"})
    assert (outcome.status, outcome.cause, fakes.calls) == ("needs_chair", "review_no_host", [])


def test_only_the_agents_branch_is_deleted_here_and_on_the_host() -> None:
    fakes = _Fakes()
    fakes.apply(_ACTION)
    assert [c for c in fakes.calls if c[0] == "delete"] == [
        ("delete", "", "/r/x", "agents/init-1/a"),
        ("delete", "u@lane-a", "/r/x", "agents/init-1/a"),
    ]


def test_a_missing_merged_at_stamps_the_time_the_chair_saw_it_merged() -> None:
    fakes = _Fakes()
    fakes.apply({**_ACTION, "merged_at": None, "host": ""})
    assert ("mark_landed", "init-1", "build", "a", _URL, "2026-10-04T09:00:00Z") in fakes.calls


def test_mark_landed_saying_already_landed_is_success() -> None:
    fakes = _Fakes(mark=Failed(1, "task a is already landed"))
    assert fakes.apply(_ACTION).status == "done"

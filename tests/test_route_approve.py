"""`cox route approve` and `decline`: the edge over draft_state, with the store and the clock faked."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import cli, draft_apply
from agent_tools.store_cli import Failed, StateRefused, StateSet

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
STAMP = "2026-09-26T12:00:00+00:00"


def initiative_md(draft: str = "true") -> str:
    return f"---\ntitle: T\ndraft: {draft}\nproposed_by: steward\n---\nProse.\n"


def ticket_md(state: str) -> str:
    return f"---\nstate: {state}\n---\nBody\n"


def make_work(tmp_path: Path, draft: str = "true", tasks: dict[str, str] | None = None) -> Path:
    root = tmp_path / "work" / "demo"
    (root / "1-build").mkdir(parents=True)
    (root / "initiative.md").write_text(initiative_md(draft))
    for task, state in (tasks or {"a": "todo", "b": "todo"}).items():
        (root / "1-build" / f"{task}.md").write_text(ticket_md(state))
    return tmp_path / "work"


def state_of(work: Path, task: str) -> str:
    return next(line for line in (work / "demo" / "1-build" / f"{task}.md").read_text().splitlines() if line.startswith("state:"))


def snapshot(work: Path) -> dict[str, str]:
    return {str(p): p.read_text() for p in sorted(work.rglob("*.md"))}


class FakeStore:
    def __init__(self, mode: str, tasks: tuple[str, ...] = ("a", "b"), results: dict[str, object] | None = None):
        self.mode, self.calls, self.row_reads = mode, [], 0
        self.tasks, self.results = tasks, results or {}

    def rows(self, initiative: str) -> list[dict]:
        self.row_reads += 1
        return [{"initiative": initiative, "task_id": t, "state": "todo"} for t in self.tasks]

    def set_state(self, initiative: str, task: str, state: str, by: str, expected: str) -> object:
        self.calls.append((initiative, task, state, by, expected))
        return self.results.get(task, StateSet({}))

    def edge(self) -> draft_apply.Store:
        return draft_apply.Store(self.mode, self.rows, self.set_state)


def approve(work: Path, store: FakeStore, task: str | None = None, by: str | None = "pat") -> int:
    return draft_apply.approve(work, "demo", task, by, store.edge(), clock=lambda: NOW, user=lambda: "os-user")


def decline(work: Path, store: FakeStore, reason: str = "not now", by: str | None = "pat") -> int:
    return draft_apply.decline(work, "demo", reason, by, store.edge(), clock=lambda: NOW, user=lambda: "os-user")


def test_approve_turns_todo_ready_and_clears_draft(tmp_path):
    work = make_work(tmp_path)
    assert approve(work, FakeStore("files")) == 0
    assert (state_of(work, "a"), state_of(work, "b")) == ("state: ready", "state: ready")
    text = (work / "demo" / "initiative.md").read_text()
    assert "draft: false" in text
    assert f"approved_by: pat\napproved_at: {STAMP}\n" in text


def test_approve_by_defaults_to_the_os_user(tmp_path):
    work = make_work(tmp_path)
    approve(work, FakeStore("files"), by=None)
    assert "approved_by: os-user\n" in (work / "demo" / "initiative.md").read_text()


def test_approve_with_task_moves_one_ticket_and_keeps_draft(tmp_path):
    work = make_work(tmp_path)
    assert approve(work, FakeStore("files"), task="a") == 0
    assert (state_of(work, "a"), state_of(work, "b")) == ("state: ready", "state: todo")
    assert "draft: true" in (work / "demo" / "initiative.md").read_text()


def test_decline_turns_todo_dropped_and_records_the_reason(tmp_path):
    work = make_work(tmp_path)
    assert decline(work, FakeStore("files"), reason="out of budget") == 0
    assert (state_of(work, "a"), state_of(work, "b")) == ("state: dropped", "state: dropped")
    text = (work / "demo" / "initiative.md").read_text()
    assert 'declined_reason: "out of budget"\n' in text
    assert "draft: false" in text


def test_refusals_write_nothing_and_exit_non_zero(tmp_path, capsys):
    cases = [
        (make_work(tmp_path / "nondraft", draft="false"), approve, "not a draft"),
        (make_work(tmp_path / "noreason"), lambda w, s: decline(w, s, reason="  "), "needs a reason"),
        (make_work(tmp_path / "notask"), lambda w, s: approve(w, s, task="zzz"), "no ticket named zzz"),
        (make_work(tmp_path / "nothing", tasks={"a": "ready"}), approve, "no todo ticket"),
    ]
    for work, act, reason in cases:
        before, store = snapshot(work), FakeStore("store")
        assert act(work, store) != 0
        assert snapshot(work) == before
        assert store.calls == []
        assert reason in capsys.readouterr().out


def test_a_missing_initiative_refuses(tmp_path, capsys):
    assert approve(tmp_path / "work", FakeStore("files")) != 0
    assert "no initiative.md" in capsys.readouterr().out


def test_store_mode_calls_set_state_once_per_moved_task_expecting_todo(tmp_path):
    work, store = make_work(tmp_path), FakeStore("store")
    assert approve(work, store) == 0
    assert store.calls == [("demo", "a", "ready", "pat", "todo"), ("demo", "b", "ready", "pat", "todo")]


def test_store_mode_decline_sets_dropped(tmp_path):
    work, store = make_work(tmp_path), FakeStore("store")
    assert decline(work, store) == 0
    assert [(c[1], c[2], c[4]) for c in store.calls] == [("a", "dropped", "todo"), ("b", "dropped", "todo")]


def test_store_mode_skips_a_task_with_no_store_row(tmp_path, capsys):
    work, store = make_work(tmp_path), FakeStore("store", tasks=("b",))
    assert approve(work, store) == 0
    assert [c[1] for c in store.calls] == ["b"]
    assert "no row for a" in capsys.readouterr().out
    assert state_of(work, "a") == "state: ready"


def test_files_mode_never_calls_the_store(tmp_path):
    work, store = make_work(tmp_path), FakeStore("files")
    assert approve(work, store) == 0
    assert decline(make_work(tmp_path / "again"), store) == 0
    assert (store.calls, store.row_reads) == ([], 0)


def test_a_precondition_failure_exits_non_zero_names_the_task_and_keeps_the_files(tmp_path, capsys):
    work = make_work(tmp_path)
    store = FakeStore("store", results={"a": StateRefused("precondition failed", "ready")})
    assert approve(work, store) != 0
    out = capsys.readouterr().out
    assert "store refused a" in out and "(now 'ready')" in out
    assert "files already written stay written" in out.lower()
    assert [c[1] for c in store.calls] == ["a"]
    assert (state_of(work, "a"), state_of(work, "b")) == ("state: ready", "state: ready")


def test_a_failed_set_state_stops_and_names_the_task(tmp_path, capsys):
    store = FakeStore("store", results={"b": Failed(1, "boom")})
    assert approve(make_work(tmp_path), store) != 0
    assert "set-state failed for b: exit 1: boom" in capsys.readouterr().out


def test_store_stop_for_each_result_class():
    assert draft_apply.store_stop("a", StateSet({})) is None
    assert "a" in (draft_apply.store_stop("a", StateRefused("no", None)) or "")
    assert "a" in (draft_apply.store_stop("a", Failed(2, "x")) or "")
    assert "a" in (draft_apply.store_stop("a", object()) or "")  # not available


def test_cli_wires_both_commands_to_the_files(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    work = make_work(ws)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    assert cli.main(["route", "approve", "demo", "--task", "a", "--by", "pat", "--profile", str(profile)]) == 0
    assert (state_of(work, "a"), state_of(work, "b")) == ("state: ready", "state: todo")
    assert cli.main(["route", "decline", "demo", "--reason", "later", "--by", "pat", "--profile", str(profile)]) == 0
    assert state_of(work, "b") == "state: dropped"
    assert cli.main(["route", "approve", "demo", "--profile", str(profile)]) != 0
    assert "not a draft" in capsys.readouterr().out

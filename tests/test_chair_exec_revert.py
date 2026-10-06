from agent_tools.chair_exec import Deps, perform

REVERT = {
    "kind": "revert_land", "initiative": "init", "phase": "p1", "repo": "repo", "pr": 12, "commit": "abc123",
    "reason": "pytest: 3 failed on main", "epoch": 0,
}
URL = "https://forge/repo/pull/99"


class FakeGit:
    def __init__(self, conflicts=(), boom=False):
        self.conflicts, self.boom = list(conflicts), boom

    def fetch_main(self, repo):
        if self.boom:
            raise OSError("network down")

    def revert_onto_branch(self, repo, branch, commit):
        return self.conflicts

    def push(self, repo, branch):
        pass


class FakeForge:
    def __init__(self, failing=()):
        self.failing = list(failing)

    def open_pr(self, repo, branch, title, body):
        return URL

    def wait_checks(self, pr):
        return self.failing

    def merge(self, pr):
        pass


def _run(action=REVERT, git=None, forge=None, wired=True):
    quarantined, resolved, recorded = [], [], []
    deps = Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
        revert_ports=(lambda a: (git or FakeGit(), forge or FakeForge())) if wired else None,
        quarantine_phase=lambda *args: quarantined.append(args),
        resolve_land=lambda *args: resolved.append(args),
    )
    return perform([action], deps, lambda: 0, False), quarantined, resolved, recorded


def test_merged_revert_quarantines_the_phase_and_raises_main_red():
    results, quarantined, _, _ = _run()
    assert quarantined == [("init", "p1", "main_red", "pytest: 3 failed on main")]
    needs = results[0]["needs_chair"]
    assert (needs["kind"], needs["cause"], needs["initiative"], needs["phase"]) == ("needs_chair", "main_red", "init", "p1")
    assert needs["reason"] == f"main went red after PR #12; reverted abc123 in {URL}"
    assert results[0]["status"] == "done"


def test_conflict_still_quarantines_and_raises_revert_failed():
    results, quarantined, _, _ = _run(git=FakeGit(conflicts=["a.py", "b.py"]))
    assert quarantined == [("init", "p1", "main_red", "pytest: 3 failed on main")]
    needs = results[0]["needs_chair"]
    assert needs["cause"] == "revert_failed"
    assert needs["reason"] == "revert conflicts in: a.py, b.py; main is still red after the revert of abc123"
    assert results[0]["status"] == "failed"


def test_failed_revert_checks_raise_revert_failed_naming_the_checks():
    results, quarantined, _, _ = _run(forge=FakeForge(failing=["lint"]))
    assert len(quarantined) == 1
    assert results[0]["needs_chair"]["reason"] == "revert checks failed: lint; main is still red after the revert of abc123"


def test_a_port_failure_still_quarantines_and_raises_revert_failed():
    results, quarantined, _, _ = _run(git=FakeGit(boom=True))
    assert len(quarantined) == 1
    assert results[0]["needs_chair"]["cause"] == "revert_failed"
    assert results[0]["needs_chair"]["reason"].startswith("revert raised OSError: network down; main is still red")


def test_resolver_is_called_with_reverted_only_when_merged():
    assert _run()[2] == [("init", "p1", "reverted")]
    assert _run(git=FakeGit(conflicts=["a.py"]))[2] == []
    assert _run(forge=FakeForge(failing=["lint"]))[2] == []
    assert _run(git=FakeGit(boom=True))[2] == []


def test_needs_chair_is_recorded_beside_the_result():
    results, _, _, recorded = _run()
    assert [r["action"]["kind"] for r in results] == ["revert_land", "needs_chair"]
    assert recorded[1]["cause"] == "main_red"


def test_unwired_revert_land_is_refused_and_writes_nothing():
    results, quarantined, resolved, _ = _run(wired=False)
    assert results[0]["status"] == "refused"
    assert "needs_chair" not in results[0]
    assert (quarantined, resolved) == ([], [])


def test_other_kinds_are_untouched():
    standby = {"kind": "standby", "initiative": "init", "epoch": 0}
    note = {"kind": "needs_chair", "initiative": "init", "cause": "x", "reason": "why", "epoch": 0}
    results, quarantined, resolved, _ = _run(standby)
    assert (results[0]["status"], results[0]["reason"]) == ("recorded", "")
    results, _, _, _ = _run(note)
    assert (results[0]["status"], results[0]["reason"]) == ("recorded", "why")
    assert (quarantined, resolved) == ([], [])

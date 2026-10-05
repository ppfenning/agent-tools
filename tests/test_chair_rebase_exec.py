from agent_tools.chair_rebase_exec import perform_rebase


class FakePort:
    # tips stand for the pushed remote tips; a failing recreate_branch leaves them unchanged, as a failed push would

    def __init__(self, tips, confirms=True, fail=()):
        self.tips = dict(tips)
        self.refs = set()
        self.confirms = confirms
        self.fail = set(fail)
        self.calls = []

    def branch_tip(self, branch):
        return self.tips.get(branch)

    def create_ref(self, ref, sha):
        self.calls.append(("create_ref", ref, sha))
        if "create_ref" in self.fail:
            raise RuntimeError("ref write refused")
        if self.confirms:
            self.refs.add(ref)

    def recreate_branch(self, branch, base):
        self.calls.append(("recreate_branch", branch, base))
        if "recreate_branch" in self.fail:
            raise RuntimeError("push rejected")
        self.tips[branch] = self.tips[base]

    def ref_exists(self, ref):
        return ref in self.refs


ACTION = {
    "kind": "rebase_phase", "initiative": "init", "phase": "p2", "branch": "init/p2",
    "tip": "abcdef1234567890", "base": "main", "epoch": 7,
}
PLANNED = {"init/p2": "abcdef1234567890", "main": "0123456789abcdef"}


def test_normal_path_backs_up_then_recreates():
    port = FakePort(PLANNED)
    assert perform_rebase(ACTION, port)["status"] == "done"
    assert port.calls == [
        ("create_ref", "backup/init/p2-abcdef12", "abcdef1234567890"),
        ("recreate_branch", "init/p2", "main"),
    ]


def test_branch_gone_is_a_no_op():
    port = FakePort({"main": "0123456789abcdef"})
    result = perform_rebase(ACTION, port)
    assert (result["status"], result["reason"]) == ("skipped", "branch init/p2 is gone")
    assert port.calls == []


def test_moved_tip_returns_needs_chair_with_the_action_epoch_and_changes_nothing():
    port = FakePort({"init/p2": "ffffffff00000000", "main": "0123456789abcdef"})
    result = perform_rebase(ACTION, port)
    assert result["status"] == "refused"
    assert result["needs_chair"] == {
        "kind": "needs_chair", "initiative": "init", "phase": "p2", "cause": "rebase_tip_moved", "epoch": 7,
        "reason": "initiative init phase p2: init/p2 is at ffffffff, not the planned abcdef12; nothing was changed",
    }
    assert port.calls == []


def test_moved_onto_base_without_our_backup_returns_needs_chair():
    port = FakePort({"init/p2": "0123456789abcdef", "main": "0123456789abcdef"})
    result = perform_rebase(ACTION, port)
    assert (result["status"], result["needs_chair"]["cause"], result["needs_chair"]["epoch"]) == ("refused", "rebase_tip_moved", 7)
    assert port.calls == []


def test_unconfirmed_backup_does_not_recreate():
    port = FakePort(PLANNED, confirms=False)
    assert perform_rebase(ACTION, port)["status"] == "failed"
    assert port.calls == [("create_ref", "backup/init/p2-abcdef12", "abcdef1234567890")]


def test_create_ref_raising_is_a_failed_result_and_does_not_recreate():
    port = FakePort(PLANNED, fail={"create_ref"})
    result = perform_rebase(ACTION, port)
    assert (result["status"], result["reason"]) == ("failed", "rebase of init/p2 failed: ref write refused")
    assert [c[0] for c in port.calls] == ["create_ref"]


def test_failed_push_is_retried_by_the_next_run():
    port = FakePort(PLANNED, fail={"recreate_branch"})
    first = perform_rebase(ACTION, port)
    port.fail.clear()
    second = perform_rebase(ACTION, port)
    assert (first["status"], first["reason"], second["status"]) == ("failed", "rebase of init/p2 failed: push rejected", "done")
    assert [c[0] for c in port.calls] == ["create_ref", "recreate_branch", "recreate_branch"]


def test_repeat_run_is_a_no_op():
    port = FakePort(PLANNED)
    first = perform_rebase(ACTION, port)
    second = perform_rebase(ACTION, port)
    assert (first["status"], second["status"]) == ("done", "skipped")
    assert second["reason"] == "branch init/p2 is already at main"
    assert [c[0] for c in port.calls] == ["create_ref", "recreate_branch"]

"""A land reads the PR's merge state before `merge` and updates a BEHIND branch first."""
from agent_tools import cli


class FakeForge:
    def __init__(self, state, waits=((True, "green"),)):
        self.state = state
        self.waits = iter(waits)
        self.calls = []

    def merge_state(self, pr):
        self.calls.append("merge_state")
        return self.state

    def update_branch(self, pr):
        self.calls.append("update_branch")

    def wait_checks(self, repo, timeout_s, ref="HEAD"):
        self.calls.append("wait_checks")
        return next(self.waits)

    def merge(self, repo, step):
        self.calls.append("merge")
        return True, "merged"


STEPS = [{"kind": "wait_checks", "branch": "origin/pr/x", "pr": 7}, {"kind": "merge", "branch": "pr/x"}]


def _walk(tmp_path, monkeypatch, forge):
    monkeypatch.setattr(cli, "_fetch_branch", lambda repo, branch: None)
    return cli._land_walk(tmp_path, STEPS, STEPS, None, None, "full", False, forge, [])


def test_behind_updates_waits_again_then_merges(tmp_path, monkeypatch):
    forge = FakeForge("BEHIND", [(True, "green"), (True, "green")])
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "update_branch", "wait_checks", "merge"]


def test_behind_with_a_failing_rewait_stops_before_merge(tmp_path, monkeypatch, capsys):
    forge = FakeForge("BEHIND", [(True, "green"), (False, "check lint failed")])
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 1
    assert "merge" not in forge.calls
    assert "wait_checks: check lint failed" in capsys.readouterr().out


def test_clean_merges_with_no_update_and_no_extra_wait(tmp_path, monkeypatch):
    forge = FakeForge("CLEAN")
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 0
    assert forge.calls == ["wait_checks", "merge_state", "merge"]


def test_dirty_stops_naming_the_state(tmp_path, monkeypatch, capsys):
    forge = FakeForge("DIRTY")
    rc, _, _ = _walk(tmp_path, monkeypatch, forge)
    assert rc == 1
    assert "DIRTY" in capsys.readouterr().out
    assert "update_branch" not in forge.calls
    assert "merge" not in forge.calls


def test_merge_gate_clean_merges():
    assert cli.merge_gate("CLEAN") == ("merge", "")


def test_merge_gate_behind_updates_then_merges():
    assert cli.merge_gate("BEHIND") == ("update_then_merge", "")


def test_merge_gate_any_other_state_stops_with_its_name():
    assert cli.merge_gate("DIRTY") == ("stop", "DIRTY")
    assert cli.merge_gate("BLOCKED") == ("stop", "BLOCKED")


def test_pr_number_prefers_the_wait_checks_pr_then_the_url():
    assert cli._pr_number(STEPS, 1, "") == 7
    assert cli._pr_number([{"kind": "merge"}], 0, "https://github.com/o/r/pull/12") == 12
    assert cli._pr_number([{"kind": "merge"}], 0, "") is None

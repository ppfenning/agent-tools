from dataclasses import replace

from agent_tools.chair_run import tick
from tests.test_chair_run import NOW, Rig

LAND = {"kind": "land", "initiative": "i", "task_id": "t1", "repo": "r", "run": "x-1", "epoch": 3}
LAUNCH = {"kind": "rescue", "initiative": "j", "task_id": "t2", "repo": "r", "epoch": 3}


class SlowLands:
    """A land sink whose land runs only when `finish` is called, as a slow land would."""

    def __init__(self):
        self.held = {}
        self.done = []

    def pending(self, repo):
        return repo in self.held

    def submit(self, action, work):
        self.held[action["repo"]] = (action, work)
        return {"action": action, "status": "in_progress", "reason": ""}

    def finish(self):
        self.done = [{**work(), "action": action} for action, work in self.held.values()]
        self.held = {}

    def collect(self):
        done, self.done = self.done, []
        return done


def test_a_slow_land_does_not_hold_up_the_launch_into_a_free_lane():
    rig, lands = Rig(), SlowLands()
    deps = rig.deps()
    deps = replace(deps, plan=lambda facts, now: [LAND, LAUNCH], exec_deps=replace(deps.exec_deps, lands=lands))

    first = tick(deps, False, NOW)
    assert [argv[1:4] for argv in rig.commands] == [["route", "launch", "rescue"]]
    assert "landing: t1 in r" in first and "lands 0" in first and "launched: " in first

    lands.finish()
    second = tick(replace(deps, plan=lambda facts, now: []), False, NOW)
    assert [a["status"] for a in rig.recorded if a["kind"] == "land"] == ["landed"]
    assert "lands 1" in second and "landing" not in second

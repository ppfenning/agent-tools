import threading
from pathlib import Path

from agent_tools import chair_land, land_repo_lease, store_cli
from agent_tools.ci_gate import CiGate

PAUSED = CiGate(True, "forge incident")


class FakeTime:
    """The clock moves only when the worker waits; a wait blocks until the test grants a tick."""

    def __init__(self) -> None:
        self.now = 0.0
        self.ticks = threading.Semaphore(0)

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.ticks.acquire(timeout=5)
        self.now += seconds


def make_worker(beat):
    fake_time = FakeTime()
    return chair_land.LandWorker(beat, 30.0, fake_time.clock, fake_time.wait), fake_time


def test_beat_is_called_more_than_once_while_a_land_that_outlasts_the_interval_runs():
    beats: list[float] = []
    twice = threading.Event()
    gate = threading.Event()

    def beat() -> None:
        beats.append(fake_time.now)
        if len(beats) >= 2:
            twice.set()

    worker, fake_time = make_worker(beat)
    submitted = worker.submit("/repo", lambda: gate.wait(5) and "landed")
    assert submitted.outcome == chair_land.Started()
    assert submitted.handle.in_progress()

    fake_time.ticks.release()
    assert twice.wait(5)
    assert submitted.handle.in_progress()
    assert beats[:2] == [0.0, 30.0]

    gate.set()
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult("landed", None, None)


def test_two_lands_for_one_repo_run_one_at_a_time_and_never_overlap():
    worker, fake_time = make_worker(lambda: None)
    log: list[str] = []
    first_gate = threading.Event()

    def first() -> str:
        log.append("first in")
        first_gate.wait(5)
        log.append("first out")
        return "one"

    def second() -> str:
        log.append("second in")
        log.append("second out")
        return "two"

    one = worker.submit("/repo", first)
    two = worker.submit("/repo", second)
    assert (one.outcome, two.outcome) == (chair_land.Started(), chair_land.Queued(1))
    assert two.handle.in_progress()

    first_gate.set()
    assert two.handle.wait(5)
    fake_time.ticks.release(10)

    assert log == ["first in", "first out", "second in", "second out"]
    assert [one.handle.result().value, two.handle.result().value] == ["one", "two"]


def test_a_land_whose_subprocess_takes_the_repo_lease_is_not_refused_by_the_workers_own_hold(monkeypatch):
    """`cox runs land` acquires `land:<repo>` as itself; the worker once held it first and every land came back busy."""
    calls: list[tuple[str, str]] = []

    def acquire(runs_dir, name, holder, ttl, steal=False):
        calls.append((name, holder))
        return store_cli.LeaseRefused(1, "chair-loop") if len(calls) > 1 else store_cli.LeaseGranted(1, holder)

    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    worker, fake_time = make_worker(lambda: None)
    submitted = worker.submit("/repo", lambda: land_repo_lease.acquire(Path("/runs"), "/repo", "land-subprocess", 1200))
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert calls == [("land:/repo", "land-subprocess")]
    assert submitted.handle.result() == chair_land.LandResult(store_cli.LeaseGranted(1, "land-subprocess"), None, None)


def test_stopping_the_worker_refuses_a_queued_land_and_any_later_one():
    worker, fake_time = make_worker(lambda: None)
    gate = threading.Event()
    running = worker.submit("/repo", lambda: gate.wait(5))
    queued = worker.submit("/repo", lambda: "never")
    worker.stop()
    assert queued.handle.wait(5)
    assert isinstance(queued.handle.result().error, chair_land.LandRefused)
    assert worker.submit("/other", lambda: "never") == chair_land.Submitted(chair_land.Refused(None), None)
    gate.set()
    assert running.handle.wait(5)
    fake_time.ticks.release(10)
    assert running.handle.result() == chair_land.LandResult(True, None, None)


def test_a_land_that_raises_anything_is_captured_in_its_result():
    worker, fake_time = make_worker(lambda: None)
    boom = SystemExit(3)

    def land() -> None:
        raise boom

    submitted = worker.submit("/repo", land)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult(None, boom, None)


def test_may_start_decides_from_plain_data():
    assert chair_land.may_start(False, 0) == chair_land.Started()
    assert chair_land.may_start(True, 2) == chair_land.Queued(3)
    assert chair_land.may_start(False, 0, PAUSED) == chair_land.Queued(1)


def test_a_paused_gate_leaves_a_queued_land_unstarted_across_beats_and_an_unpaused_one_starts_it():
    beats: list[float] = []
    worker, fake_time = make_worker(lambda: beats.append(fake_time.now))
    calls: list[str] = []

    def land() -> str:
        calls.append("landed")
        return "landed"

    submitted = worker.submit("/repo", land, PAUSED)
    for _ in range(3):
        worker.tick(PAUSED)
        fake_time.ticks.release()
    assert submitted.outcome == chair_land.Queued(1)
    assert (calls, beats, fake_time.now) == ([], [], 0.0)
    assert submitted.handle.in_progress()
    assert submitted.handle.result() is None

    worker.tick(chair_land.UNPAUSED)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert calls == ["landed"]
    assert submitted.handle.result() == chair_land.LandResult("landed", None, None)


def test_a_land_already_running_is_not_interrupted_by_a_pause_and_the_land_behind_it_is_held():
    worker, fake_time = make_worker(lambda: None)
    release = threading.Event()
    calls: list[str] = []
    running = worker.submit("/repo", lambda: release.wait(5))
    behind = worker.submit("/repo", lambda: calls.append("behind") or "behind", PAUSED)
    worker.tick(PAUSED)
    assert (running.outcome, behind.outcome) == (chair_land.Started(), chair_land.Queued(1))
    assert running.handle.in_progress()

    release.set()
    assert running.handle.wait(5)
    assert running.handle.result() == chair_land.LandResult(True, None, None)
    assert calls == []
    assert behind.handle.in_progress()

    worker.tick(chair_land.UNPAUSED)
    assert behind.handle.wait(5)
    fake_time.ticks.release(10)
    assert behind.handle.result() == chair_land.LandResult("behind", None, None)


def test_beat_due_needs_a_running_land_and_an_elapsed_interval():
    assert chair_land.beat_due(0.0, None, 30.0, 1)
    assert not chair_land.beat_due(0.0, None, 30.0, 0)
    assert not chair_land.beat_due(10.0, 0.0, 30.0, 1)
    assert chair_land.beat_due(30.0, 0.0, 30.0, 1)

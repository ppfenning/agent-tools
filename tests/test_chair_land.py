import threading
from pathlib import Path

from agent_tools import chair_land, land_repo_lease, store_cli


class FakeLease:
    """One holder per name; acquire by anyone while held is refused, renew only by the holder."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def acquire(self, runs_dir, name, holder, ttl, steal=False):
        self.calls.append(("acquire", name))
        if name in self.held:
            return store_cli.LeaseRefused(1, self.held[name])
        self.held[name] = holder
        return store_cli.LeaseGranted(1, holder)

    def renew(self, runs_dir, name, holder, epoch, ttl):
        self.calls.append(("renew", name))
        if self.held.get(name) == holder:
            return store_cli.LeaseGranted(epoch, holder)
        return store_cli.LeaseRefused(epoch, self.held.get(name))

    def release(self, runs_dir, name, holder, epoch):
        self.calls.append(("release", name))
        self.held.pop(name, None)
        return store_cli.LeaseReleased()


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


def make_worker(monkeypatch, beat):
    lease, fake_time = FakeLease(), FakeTime()
    monkeypatch.setattr(store_cli, "lease_acquire", lease.acquire)
    monkeypatch.setattr(store_cli, "lease_renew", lease.renew)
    monkeypatch.setattr(store_cli, "lease_release", lease.release)
    worker = chair_land.LandWorker(Path("/runs"), "me", 60, beat, 30.0, fake_time.clock, fake_time.wait)
    return worker, lease, fake_time


def test_beat_is_called_more_than_once_while_a_land_that_outlasts_the_interval_runs(monkeypatch):
    beats: list[float] = []
    twice = threading.Event()
    gate = threading.Event()

    def beat() -> None:
        beats.append(fake_time.now)
        if len(beats) >= 2:
            twice.set()

    worker, lease, fake_time = make_worker(monkeypatch, beat)
    submitted = worker.submit("/repo", lambda held: gate.wait(5) and "landed")
    assert submitted.outcome == chair_land.Started()
    assert submitted.handle.in_progress()

    fake_time.ticks.release()
    assert twice.wait(5)
    assert submitted.handle.in_progress()
    assert beats[:2] == [0.0, 30.0]
    assert ("renew", "land:/repo") in lease.calls

    gate.set()
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult("landed", None, None)


def test_two_lands_for_one_repo_run_one_at_a_time_and_never_overlap(monkeypatch):
    worker, lease, fake_time = make_worker(monkeypatch, lambda: None)
    log: list[str] = []
    first_gate = threading.Event()

    def first(held) -> str:
        log.append("first in")
        first_gate.wait(5)
        log.append("first out")
        return "one"

    def second(held) -> str:
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
    assert [c for c in lease.calls if c[0] == "acquire"] == [("acquire", "land:/repo")] * 2
    assert lease.held == {}


def test_the_land_uses_the_workers_lease_and_is_not_refused_by_it(monkeypatch):
    worker, lease, fake_time = make_worker(monkeypatch, lambda: None)

    def land(held: chair_land.RepoLease):
        return lease.held["land:/repo"], land_repo_lease.renew(Path("/runs"), held.repo, held.holder, held.epoch, 60)

    submitted = worker.submit("/repo", land)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result().value == ("me", store_cli.LeaseGranted(1, "me"))


def test_stopping_the_worker_while_a_land_is_in_flight_releases_its_lease_once(monkeypatch):
    worker, lease, fake_time = make_worker(monkeypatch, lambda: None)
    gate = threading.Event()
    submitted = worker.submit("/repo", lambda held: gate.wait(5))
    assert worker.stop() == ()
    assert lease.held == {}
    gate.set()
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert lease.calls.count(("release", "land:/repo")) == 1


def test_a_store_outage_fails_open_and_walks_without_the_lease(monkeypatch):
    worker, lease, fake_time = make_worker(monkeypatch, lambda: None)
    monkeypatch.setattr(store_cli, "lease_acquire", lambda runs_dir, name, holder, ttl, steal=False: store_cli.NotAvailable())
    submitted = worker.submit("/repo", lambda held: held)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.outcome == chair_land.Started()
    assert submitted.handle.result() == chair_land.LandResult(chair_land.RepoLease("/repo", "me", None), None, None)
    assert ("release", "land:/repo") not in lease.calls


def test_a_land_that_raises_anything_is_captured_and_frees_the_repo(monkeypatch):
    worker, lease, fake_time = make_worker(monkeypatch, lambda: None)
    boom = SystemExit(3)

    def land(held) -> None:
        raise boom

    submitted = worker.submit("/repo", land)
    assert submitted.handle.wait(5)
    fake_time.ticks.release(10)
    assert submitted.handle.result() == chair_land.LandResult(None, boom, None)
    assert lease.held == {}


def test_a_lease_held_by_another_process_refuses_the_land(monkeypatch):
    worker, lease, _ = make_worker(monkeypatch, lambda: None)
    lease.held["land:/repo"] = "other"
    submitted = worker.submit("/repo", lambda held: "never")
    assert (submitted.outcome, submitted.handle) == (chair_land.Refused("other"), None)


def test_may_start_decides_from_plain_data():
    granted, refused = store_cli.LeaseGranted(1, "me"), store_cli.LeaseRefused(2, "other")
    assert chair_land.may_start(False, 0, granted) == chair_land.Started()
    assert chair_land.may_start(True, 2, None) == chair_land.Queued(3)
    assert chair_land.may_start(False, 0, refused) == chair_land.Refused("other")
    assert chair_land.may_start(False, 0, store_cli.LeaseError("x")) == chair_land.Started()


def test_beat_due_needs_a_running_land_and_an_elapsed_interval():
    assert chair_land.beat_due(0.0, None, 30.0, 1)
    assert not chair_land.beat_due(0.0, None, 30.0, 0)
    assert not chair_land.beat_due(10.0, 0.0, 30.0, 1)
    assert chair_land.beat_due(30.0, 0.0, 30.0, 1)

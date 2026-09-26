import inspect
from pathlib import Path

import pytest

from agent_tools import land_lease, store_cli

RUNS = Path("/runs")
GRANTED = store_cli.LeaseGranted(7, "sess-1")


def _fakes(monkeypatch, acquire_result, release_result) -> list:
    calls = []

    def acquire(runs_dir, name, holder, ttl):
        calls.append(("acquire", runs_dir, name, holder, ttl))
        if isinstance(acquire_result, Exception):
            raise acquire_result
        return acquire_result

    def release(runs_dir, name, holder, epoch):
        calls.append(("release", runs_dir, name, holder, epoch))
        if isinstance(release_result, Exception):
            raise release_result
        return release_result

    monkeypatch.setattr(store_cli, "lease_acquire", acquire)
    monkeypatch.setattr(store_cli, "lease_release", release)
    return calls


def _run(body):
    return land_lease.run_under_land_lease(RUNS, "t1", "sess-1", 60, body)


def test_the_fakes_keep_the_real_wrapper_parameter_names():
    assert list(inspect.signature(store_cli.lease_acquire).parameters) == ["runs_dir", "name", "holder", "ttl", "steal"]
    assert list(inspect.signature(store_cli.lease_release).parameters) == ["runs_dir", "name", "holder", "epoch"]


def test_a_normal_exit_acquires_then_releases_the_same_epoch(monkeypatch):
    calls = _fakes(monkeypatch, GRANTED, store_cli.LeaseReleased())
    seen = []
    result = _run(lambda epoch: seen.append(epoch) or "done")
    assert result == land_lease.Ran("done", 7, None)
    assert seen == [7]
    assert calls == [("acquire", RUNS, "land:t1", "sess-1", 60), ("release", RUNS, "land:t1", "sess-1", 7)]


@pytest.mark.parametrize("stop", [ValueError("boom"), KeyboardInterrupt(), SystemExit(1)])
def test_an_exception_or_early_stop_still_releases_and_propagates(monkeypatch, stop):
    calls = _fakes(monkeypatch, GRANTED, store_cli.LeaseReleased())

    def body(epoch):
        raise stop

    with pytest.raises(type(stop)) as raised:
        _run(body)
    assert [c[0] for c in calls] == ["acquire", "release"]
    assert getattr(raised.value, "__notes__", []) == []


def test_a_refused_acquire_runs_no_body_and_releases_nothing(monkeypatch):
    calls = _fakes(monkeypatch, store_cli.LeaseRefused(3, "other"), store_cli.LeaseReleased())
    ran = []
    result = _run(ran.append)
    assert result == land_lease.Refused(3, "other")
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


@pytest.mark.parametrize("acquired, detail", [
    (store_cli.NotAvailable(), "no harness"),
    (store_cli.LeaseError("exit 2: bad args"), "exit 2: bad args"),
    (OSError("spawn failed"), "OSError: spawn failed"),
])
def test_an_acquire_that_is_not_a_grant_is_unavailable_and_runs_no_body(monkeypatch, acquired, detail):
    calls = _fakes(monkeypatch, acquired, store_cli.LeaseReleased())
    ran = []
    result = _run(ran.append)
    assert result == land_lease.Unavailable(detail)
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


def test_a_bug_in_acquire_propagates_instead_of_reading_as_unavailable(monkeypatch):
    calls = _fakes(monkeypatch, TypeError("wrong signature"), store_cli.LeaseReleased())
    ran = []
    with pytest.raises(TypeError, match="wrong signature"):
        _run(ran.append)
    assert ran == []
    assert [c[0] for c in calls] == ["acquire"]


_RELEASE_FAILURES = [
    (store_cli.LeaseError("exit 2: nope"), "exit 2: nope"),
    (store_cli.NotAvailable(), "no harness"),
    (store_cli.LeaseRefused(8, "other"), "held by other at epoch 8"),
    (OSError("gone"), "OSError: gone"),
]


@pytest.mark.parametrize("released, reason", _RELEASE_FAILURES)
def test_a_failing_release_is_a_warning_and_the_body_result_stands(monkeypatch, released, reason):
    _fakes(monkeypatch, GRANTED, released)
    result = _run(lambda epoch: "done")
    assert result == land_lease.Ran("done", 7, f"warning: land lease land:t1 not released: {reason}")


@pytest.mark.parametrize("released, reason", _RELEASE_FAILURES)
def test_a_failing_release_rides_as_a_note_on_the_body_exception(monkeypatch, released, reason):
    _fakes(monkeypatch, GRANTED, released)

    def body(epoch):
        raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed") as raised:
        _run(body)
    assert raised.value.__notes__ == [f"warning: land lease land:t1 not released: {reason}"]


def test_injected_callables_replace_the_wrappers(monkeypatch):
    _fakes(monkeypatch, store_cli.LeaseError("wrapper used"), store_cli.LeaseError("wrapper used"))
    seen = []
    result = land_lease.run_under_land_lease(
        RUNS, "t1", "h", 5, lambda epoch: epoch,
        acquire=lambda **kw: seen.append(kw) or store_cli.LeaseGranted(2, "h"),
        release=lambda **kw: seen.append(kw) or store_cli.LeaseReleased(),
    )
    assert result == land_lease.Ran(2, 2, None)
    assert seen == [
        {"runs_dir": RUNS, "name": "land:t1", "holder": "h", "ttl": 5},
        {"runs_dir": RUNS, "name": "land:t1", "holder": "h", "epoch": 2},
    ]

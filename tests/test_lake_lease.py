from agent_tools import store_cli
from agent_tools.lake_lease import SkippedError, SkippedHeld, Synced, sync_under_lease


class _CommitFailedException(Exception):
    """Stands in for pyiceberg's real CommitFailedException without importing pyiceberg."""


def test_a_refused_lease_skips_without_calling_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, **k: store_cli.LeaseRefused(3, "other"))
    calls = []
    assert sync_under_lease(lambda: calls.append(1), tmp_path) == SkippedHeld("other")
    assert calls == []


def test_a_granted_lease_syncs_once_and_releases_once(tmp_path, monkeypatch):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, **k: store_cli.LeaseGranted(1, "me"))
    released = []
    monkeypatch.setattr(store_cli, "lease_release", lambda runs_dir, name, holder, epoch: released.append(epoch))
    assert sync_under_lease(lambda: "done", tmp_path) == Synced("done")
    assert released == [1]


def test_a_sync_that_raises_releases_the_lease_and_reports_the_exception_name(tmp_path, monkeypatch):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, **k: store_cli.LeaseGranted(1, "me"))
    released = []
    monkeypatch.setattr(store_cli, "lease_release", lambda runs_dir, name, holder, epoch: released.append(epoch))

    def boom():
        raise ValueError("nope")

    assert sync_under_lease(boom, tmp_path) == SkippedError("ValueError")
    assert released == [1]


def test_a_commit_failed_exception_is_retried_once_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda *a, **k: store_cli.LeaseGranted(1, "me"))
    released = []
    monkeypatch.setattr(store_cli, "lease_release", lambda runs_dir, name, holder, epoch: released.append(epoch))
    monkeypatch.setattr("agent_tools.lake_lease._commit_failed_exception", lambda: _CommitFailedException)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise _CommitFailedException("conflict")
        return "done"

    assert sync_under_lease(flaky, tmp_path) == Synced("done")
    assert len(calls) == 2
    assert released == [1]

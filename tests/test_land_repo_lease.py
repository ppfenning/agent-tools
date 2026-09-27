from agent_tools import land_repo_lease, store_cli


def test_lease_name_is_land_prefixed_with_the_repo():
    assert land_repo_lease.lease_name("/repo") == "land:/repo"


def test_refusal_message_is_the_exact_string():
    assert land_repo_lease.refusal_message("/repo", "pid 1 on host") == "land: refusing, pid 1 on host is landing in /repo"


def test_acquire_calls_store_cli_lease_acquire_with_the_repo_lease_name(monkeypatch, tmp_path):
    seen = []

    def fake_acquire(runs_dir, name, holder, ttl):
        seen.append((runs_dir, name, holder, ttl))
        return store_cli.LeaseGranted(1, "me")

    monkeypatch.setattr(store_cli, "lease_acquire", fake_acquire)
    result = land_repo_lease.acquire(tmp_path, "/repo", "me", 60)

    assert seen == [(tmp_path, "land:/repo", "me", 60)]
    assert result == store_cli.LeaseGranted(1, "me")


def test_acquire_returns_a_refusal_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(store_cli, "lease_acquire", lambda runs_dir, name, holder, ttl: store_cli.LeaseRefused(2, "other"))
    result = land_repo_lease.acquire(tmp_path, "/repo", "me", 60)
    assert result == store_cli.LeaseRefused(2, "other")


def test_renew_calls_store_cli_lease_renew_with_the_repo_lease_name_and_epoch(monkeypatch, tmp_path):
    seen = []

    def fake_renew(runs_dir, name, holder, epoch, ttl):
        seen.append((runs_dir, name, holder, epoch, ttl))
        return store_cli.LeaseGranted(4, "me")

    monkeypatch.setattr(store_cli, "lease_renew", fake_renew)
    result = land_repo_lease.renew(tmp_path, "/repo", "me", 4, 60)

    assert seen == [(tmp_path, "land:/repo", "me", 4, 60)]
    assert result == store_cli.LeaseGranted(4, "me")


def test_release_calls_store_cli_lease_release_with_the_repo_lease_name_and_epoch(monkeypatch, tmp_path):
    seen = []

    def fake_release(runs_dir, name, holder, epoch):
        seen.append((runs_dir, name, holder, epoch))
        return store_cli.LeaseReleased()

    monkeypatch.setattr(store_cli, "lease_release", fake_release)
    result = land_repo_lease.release(tmp_path, "/repo", "me", 4)

    assert seen == [(tmp_path, "land:/repo", "me", 4)]
    assert result == store_cli.LeaseReleased()

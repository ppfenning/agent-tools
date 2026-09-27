from agent_tools import chair_housekeeping as hk


def _runner(fail=None, boom=None):
    seen: list[list[str]] = []

    def run(argv):
        seen.append(argv)
        if boom and boom in argv:
            raise OSError("no such file")
        return (1, "sync exploded\nmore") if fail and fail in argv else (0, "fine\nmore")

    return run, seen


def test_failed_sync_does_not_stop_prune():
    run, seen = _runner(fail="lake")
    status, reason = hk.run_housekeeping(run, "/t", lambda: True)
    assert seen == [hk.sync_argv(), hk.prune_argv("/t")]
    assert status == "failed"
    assert reason == (
        "lake sync: FAILED exit 1 sync exploded; prune: ok fine; "
        f"clean skipped: {hk.CLEAN_MISSING}"
    )


def test_all_ok_is_done():
    run, _ = _runner()
    status, reason = hk.run_housekeeping(run, "/t", lambda: True)
    assert status == "done"
    assert reason.startswith("lake sync: ok fine; prune: ok fine; clean skipped: ")


def test_prune_unavailable_is_skipped_not_failed():
    run, seen = _runner()
    status, reason = hk.run_housekeeping(run, "/t", lambda: False)
    assert status == "done"
    assert "prune skipped: subcommand not available" in reason
    assert hk.prune_argv("/t") not in seen


def test_prune_raising_is_reported_for_prune_only():
    run, seen = _runner(boom="prune")
    status, reason = hk.run_housekeeping(run, "/t", lambda: True)
    assert status == "failed"
    assert "prune: FAILED exit 1 OSError: no such file" in reason
    assert reason.startswith("lake sync: ok")
    assert "clean skipped:" in reason


def test_prune_argv_literal():
    assert hk.prune_argv("/traces") == [
        "python", "-m", "harness.store_backfill_traces", "prune", "/traces", "--older-than", "7",
    ]


def test_clean_argv_runs_nothing_destructive():
    assert hk.clean_argv() is None


def test_join_reason_keeps_failed_names():
    lines = [f"{n}: FAILED exit 1 " + "x" * 80 for n in ("lake sync", "prune", "clean")]
    reason = hk.join_reason(lines)
    assert len(reason) <= 200
    assert all(f"{n}: FAILED" in reason for n in ("lake sync", "prune", "clean"))

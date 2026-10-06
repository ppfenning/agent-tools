from datetime import UTC, datetime

from agent_tools.chair_idle_stall import diagnose, is_stalled, minutes_between

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
AT_15 = "2026-10-05T11:45:00Z"
AT_14 = "2026-10-05T11:46:00Z"
AT_16 = "2026-10-05T11:44:00Z"


def make(**over):
    base = {
        "free_lanes": 1,
        "ready": 1,
        "queued": 0,
        "last_progress_at": AT_15,
        "stall_minutes": 15,
        "hosts": [],
        "empty_stubs": [],
        "lands_waiting": [],
        "blocked_ready": [],
        "open_signature": None,
        "open_diagnosis": None,
    }
    return {**base, **over}


def land(**over):
    base = {
        "run": "r1",
        "pr": "7",
        "waiting_since": AT_16,
        "checks_started": True,
        "forge_status": None,
    }
    return {**base, **over}


def test_minutes_between_known_gap():
    assert minutes_between(AT_15, NOW) == 15.0


def test_minutes_between_none_and_garbage():
    assert minutes_between(None, NOW) is None
    assert minutes_between("yesterday", NOW) is None


def test_stalled_at_fifteen_minutes():
    assert is_stalled(make(), NOW) is True


def test_not_stalled_at_fourteen_minutes():
    assert is_stalled(make(last_progress_at=AT_14), NOW) is False


def test_no_free_lanes_not_stalled():
    assert is_stalled(make(free_lanes=0), NOW) is False


def test_no_work_waiting_not_stalled():
    assert is_stalled(make(ready=0, queued=0), NOW) is False


def test_unknown_progress_age_not_stalled():
    assert is_stalled(make(last_progress_at=None), NOW) is False


def test_land_waiting_sixteen_minutes_stalls_without_free_lanes():
    assert is_stalled(make(free_lanes=0, ready=0, lands_waiting=[land()]), NOW) is True


def test_diagnose_host():
    got = diagnose(
        make(hosts=[{"host": "h1", "ok": True, "detail": "fine"}, {"host": "h2", "ok": False, "detail": "no beat"}])
    )
    assert got == {
        "kind": "host",
        "subject": "h2",
        "text": "host h2 failed its check: no beat",
        "signature": "host:h2",
    }


def test_diagnose_stub():
    got = diagnose(make(empty_stubs=["alpha", "beta"]))
    assert got == {
        "kind": "stub",
        "subject": "alpha",
        "text": "the intake has an initiative stub and no tasks: alpha",
        "signature": "stub:alpha",
    }


def test_diagnose_land_ci_quotes_forge_status():
    got = diagnose(make(lands_waiting=[land(checks_started=False, forge_status="no checks reported")]))
    assert got == {
        "kind": "land_ci",
        "subject": "r1",
        "text": "run r1 pr 7 has not started its checks, forge status: 'no checks reported'",
        "signature": "land_ci:r1",
    }


def test_diagnose_land_ci_without_forge_status():
    got = diagnose(make(lands_waiting=[land(checks_started=False)]))
    assert got["text"] == "run r1 pr 7 has not started its checks"


def test_diagnose_blocked_needs():
    got = diagnose(make(blocked_ready=[{"task": "t9", "unlanded_needs": ["t1", "t2"]}]))
    assert got == {
        "kind": "blocked_needs",
        "subject": "t9",
        "text": "task t9 waits on unlanded needs: t1, t2",
        "signature": "blocked_needs:t9",
    }


def test_diagnose_unknown_carries_counts():
    got = diagnose(make(free_lanes=2, ready=3, queued=4, lands_waiting=[land()]))
    assert got == {
        "kind": "unknown",
        "subject": "chair",
        "text": "unknown free_lanes=2 ready=3 queued=4 lands_waiting=1",
        "signature": "unknown:chair",
    }


def test_failed_host_beats_empty_stub():
    got = diagnose(make(hosts=[{"host": "h2", "ok": False, "detail": "x"}], empty_stubs=["alpha"]))
    assert got["kind"] == "host"


def test_unknown_signature_ignores_counts():
    a = diagnose(make(free_lanes=1, ready=1))
    b = diagnose(make(free_lanes=5, ready=0, queued=9))
    assert a["signature"] == b["signature"] == "unknown:chair"

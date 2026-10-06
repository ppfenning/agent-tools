import json
from pathlib import Path

import pytest

from agent_tools import cli, dash_detail_health

FIXTURE = Path(__file__).parent / "fixtures" / "dash_detail_health_v1.json"
NOW = "2026-10-05T04:30:00Z"
MACHINES = [
    {
        "name": "mini", "state": "active", "lanes_in_use": 1, "capacity": 2,
        "login_ok": True, "login_checked_at": "2026-10-05T04:00:00Z", "beat_age_s": 12, "checkouts": {},
    },
]
LEASE_ROW = {"holder": "chair@mini:4242", "expires_at": "2026-10-05T05:00:00+00:00"}
STORE_RESULT = {"ok": True, "detail": "store opened"}
REAL_TIMED_STORE_READ = dash_detail_health._timed_store_read
REAL_MACHINES = dash_detail_health._machines
REAL_LEASE_ROW = dash_detail_health._lease_row


@pytest.fixture(autouse=True)
def _offline_schema_1_edges(monkeypatch):
    """Every `build` call in this module reads no feed, store or lease: the three schema-1 edges return literals."""
    monkeypatch.setattr(dash_detail_health, "_machines", lambda runs_dir, work_dir, now, profile: (MACHINES, None))
    monkeypatch.setattr(dash_detail_health, "_timed_store_read", lambda runs_dir: (STORE_RESULT, 12))
    monkeypatch.setattr(dash_detail_health, "_lease_row", lambda runs_dir: (LEASE_ROW, None))


def _assert_same_shape(got, want, path="$"):
    """`got` has every key of `want`, recursively, and the same JSON type at each place; lists compare their first item."""
    assert type(got) is type(want), f"{path}: {type(got).__name__} is not {type(want).__name__}"
    if isinstance(want, dict):
        assert want.keys() <= got.keys(), f"{path}: missing {sorted(want.keys() - got.keys())}"
        for key in want:
            _assert_same_shape(got[key], want[key], f"{path}.{key}")
    elif isinstance(want, list) and want:
        assert got, f"{path}: empty list"
        _assert_same_shape(got[0], want[0], f"{path}[0]")


LITERALS = {
    "_store_drift": {"ok": True, "drift": [{"initiative": "x", "task_id": "1", "kind": "state"}]},
    "_lake_doctor": {"ok": True, "verdict": "ok", "checks": [{"name": "catalog", "status": "ok", "detail": "fine"}]},
    "_object_store": {"ok": True, "reachable": True, "reason": "ok", "endpoint": "http://minio:9000"},
    "_last_backup": {"ok": False, "error": "no backup reader exists in agent_tools/"},
    "_housekeeping": {"ok": True, "last_at": "2026-09-27T00:00:00Z"},
}


def _patch_all(monkeypatch):
    for name in ("_store_drift", "_lake_doctor", "_object_store"):
        monkeypatch.setattr(dash_detail_health, name, lambda *args, value=LITERALS[name]: value)
    monkeypatch.setattr(dash_detail_health, "_housekeeping", lambda runs_dir: LITERALS["_housekeeping"])
    monkeypatch.setattr(dash_detail_health, "_last_backup", lambda: LITERALS["_last_backup"])


def test_build_assembles_the_five_monkeypatched_readers_into_one_dict(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    _patch_all(monkeypatch)
    result = dash_detail_health.build(tmp_path / "runs", tmp_path, "2026-09-28T00:00:00Z", provider={}, harness=None)
    assert {k: result[k] for k in ("store_drift", "lake_doctor", "object_store", "last_backup")} == {
        "store_drift": LITERALS["_store_drift"],
        "lake_doctor": LITERALS["_lake_doctor"],
        "object_store": LITERALS["_object_store"],
        "last_backup": LITERALS["_last_backup"],
    }
    assert result["housekeeping_panel"] == LITERALS["_housekeeping"]
    assert result["housekeeping"] == {"last_run_at": "2026-09-27T00:00:00Z", "age_hours": 24.0}


def test_build_confines_a_raising_reader_to_its_own_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    _patch_all(monkeypatch)

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(dash_detail_health, "_last_backup", _boom)
    result = dash_detail_health.build(tmp_path / "runs", tmp_path, "2026-09-28T00:00:00Z", provider={}, harness=None)
    assert {k: result[k] for k in ("store_drift", "lake_doctor", "object_store", "last_backup", "housekeeping_panel")} == {
        "store_drift": LITERALS["_store_drift"],
        "lake_doctor": LITERALS["_lake_doctor"],
        "object_store": LITERALS["_object_store"],
        "last_backup": {"ok": False, "error": "boom"},
        "housekeeping_panel": LITERALS["_housekeeping"],
    }


def test_the_schema_1_snapshot_has_every_fixture_key_with_the_same_json_type():
    snapshot = dash_detail_health.schema_1(MACHINES, LEASE_ROW, STORE_RESULT, 12, "2026-10-05T00:00:00Z", NOW)
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _assert_same_shape(snapshot, want)
    assert (snapshot["schema"], snapshot["kind"]) == (1, "health")
    assert snapshot == want


def test_schema_1_reads_an_unchecked_login_a_missing_lease_and_no_housekeeping_as_not_ok_and_typed_empties():
    unchecked = [{**MACHINES[0], "login_ok": None, "login_checked_at": ""}]
    snapshot = dash_detail_health.schema_1(unchecked, None, {"ok": False, "detail": "no store"}, 3, None, NOW)
    assert snapshot["logins"] == [{"provider": "claude", "host": "mini", "ok": False, "detail": "login not checked"}]
    assert snapshot["chair_lease"] == {"holder": "", "expires_at": "", "ok": False}
    assert snapshot["housekeeping"] == {"last_run_at": "", "age_hours": dash_detail_health.NEVER_AGE_HOURS}
    assert snapshot["store"] == {"ok": False, "detail": "no store", "latency_ms": 3}


def test_a_lease_that_expired_before_the_snapshot_is_not_ok():
    expired = {"holder": "chair@mini:4242", "expires_at": "2026-10-05T04:00:00+00:00"}
    assert dash_detail_health._chair_lease(expired, dash_detail_health._parse_now(NOW)) == {
        "holder": "chair@mini:4242", "expires_at": "2026-10-05T04:00:00+00:00", "ok": False,
    }


def test_the_timed_store_read_reports_elapsed_milliseconds_from_the_clock_it_is_given(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(dash_detail_health.run_store, "_open", lambda runs_dir: None)
    ticks = iter([10.0, 10.25])
    result, elapsed_ms = REAL_TIMED_STORE_READ(tmp_path, lambda: next(ticks))
    assert (result["ok"], elapsed_ms) == (False, 250)


def test_cox_dash_detail_health_prints_json_whose_kind_is_health(monkeypatch, tmp_path: Path, capsys):
    """The real feed, store and lease edges run, stubbed one layer down, so the wiring itself is exercised."""
    routing = tmp_path / "routing.yaml"
    routing.write_text("workspace_dir: /nowhere\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing))
    _patch_all(monkeypatch)
    monkeypatch.setattr(dash_detail_health, "_machines", REAL_MACHINES)
    monkeypatch.setattr(dash_detail_health, "_lease_row", REAL_LEASE_ROW)
    monkeypatch.setattr(dash_detail_health, "_timed_store_read", REAL_TIMED_STORE_READ)
    seen = {}

    def _gather_feed(runs_dir, work_dir, now, profile=None):
        seen["profile"] = profile
        return {"machines": MACHINES}

    monkeypatch.setattr(dash_detail_health.dash_feed, "gather_feed", _gather_feed)
    monkeypatch.setattr(dash_detail_health.run_store, "_open", lambda runs_dir: None)
    lease = {dash_detail_health.chair.LEASE_NAME: (LEASE_ROW["holder"], LEASE_ROW["expires_at"], "")}
    monkeypatch.setattr(dash_detail_health.run_store, "_lease_table", lambda runs_dir, window: lease)
    assert cli.main(["dash", "--detail", "health", "--runs-dir", str(tmp_path), "--work-dir", str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["kind"], printed["schema"], printed["errors"]) == ("health", 1, [])
    assert printed["hosts"] == [{"name": "mini", "state": "active", "lanes_in_use": 1, "capacity": 2}]
    assert printed["chair_lease"]["holder"] == "chair@mini:4242"
    assert seen["profile"]["workspace_dir"] == "/nowhere"


def test_a_raising_feed_and_lease_read_are_named_in_errors_not_shown_as_an_empty_fleet(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    _patch_all(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(dash_detail_health, "_machines", REAL_MACHINES)
    monkeypatch.setattr(dash_detail_health, "_lease_row", REAL_LEASE_ROW)
    monkeypatch.setattr(dash_detail_health.dash_feed, "gather_feed", _boom)
    monkeypatch.setattr(dash_detail_health.run_store, "_lease_table", _boom)
    result = dash_detail_health.build(tmp_path / "runs", tmp_path, NOW, provider={}, harness=None)
    assert result["errors"] == ["machines: RuntimeError: down", "chair_lease: RuntimeError: down"]
    assert (result["hosts"], result["chair_lease"]["ok"]) == ([], False)


def test_store_drift_reads_a_stateless_task_as_todo_and_reports_intake_drift(monkeypatch, tmp_path: Path):
    (tmp_path / "work" / "ini" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "ini" / "p1" / "t1.md").write_text("---\nid: t1\n---\nbody\n")
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text("an idea\n")
    rows = [
        {"kind": "task", "initiative": "ini", "task_id": "t1", "state": "todo"},
        {"kind": "intake", "initiative": "intake", "task_id": "idea", "state": "landed"},
    ]
    monkeypatch.setattr(dash_detail_health.run_store, "read_queue", lambda runs_dir: rows)
    assert dash_detail_health._store_drift(tmp_path / "runs", tmp_path) == {
        "ok": True,
        "drift": [
            {"initiative": "intake", "task_id": "intake/idea.md", "kind": "state", "file_state": "queued", "store_state": "landed"},
        ],
    }


def test_lake_panels_without_a_provider_profile_report_an_error_not_defaults(tmp_path: Path):
    expected = {"ok": False, "error": dash_detail_health.NO_PROFILE}
    assert dash_detail_health._lake_doctor(tmp_path, None) == expected
    assert dash_detail_health._object_store(tmp_path, None, None) == expected


def test_object_store_reports_the_live_probe(monkeypatch, tmp_path: Path):
    class _Check:
        readable, reason = False, "root unreachable"

    monkeypatch.setattr(dash_detail_health.run_store, "parquet_readable", lambda root, harness: _Check())
    result = dash_detail_health._object_store(tmp_path, {}, Path("/harness/.venv/bin/python"))
    assert result == {"ok": False, "reachable": False, "reason": "root unreachable", "endpoint": None}


def test_object_store_passes_the_routing_profile_harness_to_parquet_readable(monkeypatch, tmp_path: Path):
    """The harness argument must reach `parquet_readable` unchanged: dropping it (passing `None`) turns
    a pyarrow-less install that the harness venv could read into a false "pyarrow missing" failure,
    disagreeing with `cox setup doctor`'s own `run_store.parquet_readable(root, run_store.harness_python(profile))` call."""
    captured = {}
    harness = Path("/harness/.venv/bin/python")

    class _Check:
        readable, reason = True, "through the harness"

    def _fake_parquet_readable(root, given_harness):
        captured["harness"] = given_harness
        return _Check()

    monkeypatch.setattr(dash_detail_health.run_store, "parquet_readable", _fake_parquet_readable)
    result = dash_detail_health._object_store(tmp_path, {}, harness)
    assert captured["harness"] is harness
    assert result == {"ok": True, "reachable": True, "reason": "through the harness", "endpoint": None}


def test_build_threads_the_harness_argument_into_the_object_store_panel(monkeypatch, tmp_path: Path):
    captured = {}
    harness = Path("/harness/.venv/bin/python")
    monkeypatch.setattr(dash_detail_health, "_store_drift", lambda *a: LITERALS["_store_drift"])
    monkeypatch.setattr(dash_detail_health, "_lake_doctor", lambda *a: LITERALS["_lake_doctor"])
    monkeypatch.setattr(dash_detail_health, "_housekeeping", lambda runs_dir: LITERALS["_housekeeping"])
    monkeypatch.setattr(dash_detail_health, "_last_backup", lambda: LITERALS["_last_backup"])

    def _fake_object_store(runs_dir, provider, given_harness):
        captured["harness"] = given_harness
        return LITERALS["_object_store"]

    monkeypatch.setattr(dash_detail_health, "_object_store", _fake_object_store)
    dash_detail_health.build(tmp_path / "runs", tmp_path, "2026-09-28T00:00:00Z", provider={}, harness=harness)
    assert captured["harness"] is harness


def test_build_resolves_the_profile_itself_when_called_with_three_arguments(monkeypatch, tmp_path: Path):
    """The caller planned for `build` — `cox dash --detail health` — calls `build(runs_dir, work_dir,
    now)` with no `provider`/`harness` keywords at all. Without this resolution, the lake doctor and
    object-store panels would always report `NO_PROFILE` once that caller is wired up, which is the
    reason run 2's build was sent back."""
    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text("storage_url: postgresql://example/db\n")
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(f"provider_profile: {provider_path}\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing_path))

    captured = {}

    def _fake_lake_doctor(runs_dir, provider):
        captured["provider"] = provider
        return LITERALS["_lake_doctor"]

    monkeypatch.setattr(dash_detail_health, "_store_drift", lambda *a: LITERALS["_store_drift"])
    monkeypatch.setattr(dash_detail_health, "_lake_doctor", _fake_lake_doctor)
    monkeypatch.setattr(dash_detail_health, "_object_store", lambda *a: LITERALS["_object_store"])
    monkeypatch.setattr(dash_detail_health, "_housekeeping", lambda runs_dir: LITERALS["_housekeeping"])
    monkeypatch.setattr(dash_detail_health, "_last_backup", lambda: LITERALS["_last_backup"])

    dash_detail_health.build(tmp_path / "runs", tmp_path, "2026-09-28T00:00:00Z")

    assert captured["provider"] == dash_detail_health.store_url.read_provider_profile(provider_path)
    assert captured["provider"]

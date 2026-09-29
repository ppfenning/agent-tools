from pathlib import Path

from agent_tools import dash_detail_health

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
    assert result == {
        "store_drift": LITERALS["_store_drift"],
        "lake_doctor": LITERALS["_lake_doctor"],
        "object_store": LITERALS["_object_store"],
        "last_backup": LITERALS["_last_backup"],
        "housekeeping": LITERALS["_housekeeping"],
    }


def test_build_confines_a_raising_reader_to_its_own_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGENT_TOOLS_PROFILE", raising=False)
    _patch_all(monkeypatch)

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(dash_detail_health, "_last_backup", _boom)
    result = dash_detail_health.build(tmp_path / "runs", tmp_path, "2026-09-28T00:00:00Z", provider={}, harness=None)
    assert result == {
        "store_drift": LITERALS["_store_drift"],
        "lake_doctor": LITERALS["_lake_doctor"],
        "object_store": LITERALS["_object_store"],
        "last_backup": {"ok": False, "error": "boom"},
        "housekeeping": LITERALS["_housekeeping"],
    }


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

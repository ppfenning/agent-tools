from agent_tools.store_drill_compare import Snapshot, compare


def snap(runs: int, work_items: int, chair_actions: int, version: str = "v7") -> Snapshot:
    return Snapshot(version, {"runs": runs, "work_items": work_items, "chair_actions": chair_actions})


def failed(result) -> list[str]:
    return [f.check for f in result.findings if not f.ok]


def test_exact_match_passes():
    result = compare(snap(1000, 50, 200), snap(1000, 50, 200), 0.05)
    assert result.passed and failed(result) == []


def test_restored_inside_tolerance_passes():
    assert compare(snap(960, 50, 200), snap(1000, 50, 200), 0.05).passed


def test_restored_just_outside_tolerance_fails():
    result = compare(snap(949, 50, 200), snap(1000, 50, 200), 0.05)
    assert not result.passed and failed(result) == ["runs"]


def test_restored_above_live_fails():
    result = compare(snap(1001, 50, 200), snap(1000, 50, 200), 0.05)
    assert not result.passed and failed(result) == ["runs"]


def test_schema_version_mismatch_fails():
    result = compare(snap(1000, 50, 200, "v6"), snap(1000, 50, 200, "v7"), 0.05)
    assert not result.passed and failed(result) == ["schema_version"]


def test_missing_table_fails():
    restored = Snapshot("v7", {"runs": 1000, "work_items": 50})
    result = compare(restored, snap(1000, 50, 200), 0.05)
    assert not result.passed and failed(result) == ["chair_actions"]


def test_zero_live_with_zero_restored_passes():
    assert compare(snap(1000, 0, 200), snap(1000, 0, 200), 0.05).passed

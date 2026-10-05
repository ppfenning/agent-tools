from agent_tools.chair_steer import shared_paths, steer_check


def _run(id: str, surfaces: list[str], repo: str = "r") -> dict:
    return {"id": id, "repo": repo, "surfaces": surfaces}


def test_equal_files_overlap():
    assert shared_paths(["a.py"], ["a.py"]) == ["a.py"]


def test_disjoint_files_return_none():
    assert steer_check("cand", "r", ["a.py"], [_run("peer", ["b.py"])], {}) is None


def test_directory_overlaps_file_and_reports_file():
    assert shared_paths(["src/ui/"], ["src/ui/regatta.rs"]) == ["src/ui/regatta.rs"]


def test_snapshots_ignored():
    assert shared_paths(["src/ui/snapshots/"], ["src/ui/snapshots/a.snap"]) == []


def test_snapshots_mid_path_ignored():
    assert shared_paths(["a/snapshots/b/c.rs"], ["a/"]) == []


def test_lock_file_ignored():
    assert shared_paths(["Cargo.lock"], ["Cargo.lock"]) == []


def test_package_lock_ignored():
    assert shared_paths(["web/package-lock.json"], ["web/"]) == []


def test_new_marker_stripped():
    assert shared_paths(["a.py (new)"], ["a.py"]) == ["a.py"]


def test_other_repo_skipped():
    assert steer_check("cand", "r", ["a.py"], [_run("peer", ["a.py"], repo="s")], {}) is None


def test_own_id_skipped():
    assert steer_check("cand", "r", ["a.py"], [_run("cand", ["a.py"])], {}) is None


def test_first_overlap_in_order_wins():
    running = [_run("none", ["z.py"]), _run("first", ["a.py"]), _run("second", ["a.py"])]
    assert steer_check("cand", "r", ["a.py"], running, {})["other"] == "first"


def test_streak_two_steers_clear():
    action = steer_check("cand", "r", ["a.py"], [_run("peer", ["a.py"])], {"cand|peer": 2})
    assert action == {"kind": "steer_clear", "initiative": "cand", "other": "peer", "paths": ["a.py"]}


def test_streak_three_needs_chair():
    action = steer_check("cand", "r", ["src/x.rs"], [_run("peer", ["src/x.rs"])], {"cand|peer": 3})
    assert action == {
        "kind": "needs_chair",
        "initiative": "cand",
        "cause": "steer_deferred",
        "reason": "cand shares src/x.rs with running peer; the pair has been deferred three ticks",
    }

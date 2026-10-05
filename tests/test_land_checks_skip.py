from types import SimpleNamespace

from agent_tools import cli, land

SKIP = "checks: tests skipped (pytest exit 5, no tests ran)"
FORGE = SimpleNamespace(push=lambda repo, branch: (True, f"pushed {branch}"))


def _exits(monkeypatch, *codes):
    it = iter(codes)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=next(it), stdout="no tests ran in 0.00s", stderr=""))


def _walk(tmp_path, checks):
    steps = [{"kind": "checks", "checks": checks}, {"kind": "push", "branch": "pr"}]
    return cli._land_walk(tmp_path, steps, steps, None, None, "merge", False, FORGE, [])


def test_check_exit_rules():
    assert land.check_exit(["pytest", "-q"], 0) == "pass"
    assert land.check_exit(["pytest", "-q"], 5) == "skip"
    assert land.check_exit(["python3", "-m", "pytest"], 5) == "skip"
    assert land.check_exit(["uv", "run", "pytest"], 5) == "skip"
    assert land.check_exit(["coverage", "run", "pytest"], 5) == "fail"
    assert land.check_exit(["pytest", "-q"], 1) == "fail"
    assert land.check_exit(["ruff", "check", "."], 5) == "fail"


def test_pytest_exit_5_skips_names_the_skip_and_the_land_pushes(tmp_path, monkeypatch, capsys):
    _exits(monkeypatch, 5)
    _, reached, _ = _walk(tmp_path, [("tests", ["pytest", "-q"])])
    out = capsys.readouterr().out.splitlines()
    assert reached == ["checks", "push"]
    assert out[:3] == ["checks: 1 checks passed", SKIP, "push: pushed pr"]


def test_pytest_exit_1_refuses(tmp_path, monkeypatch):
    _exits(monkeypatch, 1)
    ok, detail = cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", ["pytest", "-q"])]})
    assert (ok, detail) == (False, "tests: no tests ran in 0.00s")


def test_non_pytest_exit_5_still_refuses(tmp_path, monkeypatch):
    _exits(monkeypatch, 5)
    _, reached, _ = _walk(tmp_path, [("tests", ["ruff", "check", "."])])
    assert reached == ["checks"]


def test_a_skip_before_a_failure_is_still_named(tmp_path, monkeypatch):
    _exits(monkeypatch, 5, 1)
    ok, detail = cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", ["pytest"]), ("lint", ["ruff"])]})
    assert (ok, detail) == (False, f"lint: no tests ran in 0.00s\n{SKIP}")

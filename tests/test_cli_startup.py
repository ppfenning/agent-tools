"""Guard lazy command imports: the heavy modules stay unloaded and startup stays inside a ceiling."""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

HEAVY_MODULES = ("psycopg", "duckdb", "pyarrow")

# Ceilings for the median wall time of one `cox` process, in seconds.
# Measured on the finished code (two sessions, 7 timed runs each, macOS, warm cache):
#   cox --help                  median 49 ms  (about 72 ms before the command modules went lazy)
#   cox route chair status      median 55 ms
# Ceilings are about 5x the measured medians, so a slow CI runner passes and a return to eager
# heavy imports does not.
HELP_CEILING_S = 0.25
CHAIR_STATUS_CEILING_S = 0.30

WARMUP_RUNS = 1
TIMED_RUNS = 7

_ISOLATION_SNIPPET = (
    f"import json, sys, agent_tools.cli\nprint(json.dumps([m for m in {HEAVY_MODULES!r} if m in sys.modules]))\n"
)


def _env() -> dict[str, str]:
    """The parent environment with the repo root first on PYTHONPATH, so the subprocess imports this tree."""
    inherited = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (str(REPO_ROOT), inherited) if p)}


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, cwd=REPO_ROOT, env=_env(), check=False)


def _cox(*args: str) -> list[str]:
    return [sys.executable, "-m", "agent_tools.cli", *args]


def _median_seconds(argv: list[str], runs: int) -> float:
    """Median wall time of `runs` invocations after discarded warm-up runs; any non-zero exit fails."""

    def timed() -> float:
        start = time.perf_counter()
        proc = _run(argv)
        elapsed = time.perf_counter() - start
        assert proc.returncode == 0, f"{argv} exited {proc.returncode}: {proc.stderr}{proc.stdout}"
        return elapsed

    for _ in range(WARMUP_RUNS):
        timed()
    return statistics.median(timed() for _ in range(runs))


def test_cli_import_does_not_load_heavy_modules() -> None:
    proc = _run([sys.executable, "-c", _ISOLATION_SNIPPET])
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    assert loaded == [], f"importing agent_tools.cli loaded {loaded}"


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "profile.yaml"
    path.write_text(f"workspace_dir: {workspace}\n", encoding="utf-8")
    return path


def test_help_startup_under_ceiling() -> None:
    median = _median_seconds(_cox("--help"), TIMED_RUNS)
    assert median < HELP_CEILING_S, f"cox --help median {median:.3f}s, ceiling {HELP_CEILING_S}s"


def test_route_chair_status_startup_under_ceiling(profile: Path) -> None:
    argv = _cox("route", "chair", "status", "--profile", str(profile))
    median = _median_seconds(argv, TIMED_RUNS)
    assert median < CHAIR_STATUS_CEILING_S, (
        f"cox route chair status median {median:.3f}s, ceiling {CHAIR_STATUS_CEILING_S}s"
    )

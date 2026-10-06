import subprocess
import sys

import pytest

_CODE = """
import sys
import agent_tools.{module}
print(sorted({{m.split(".")[0] for m in sys.modules}} & {{"psycopg", "duckdb", "pyarrow"}}))
"""

_BLOCKED = """
import sys
sys.modules["pyarrow"] = None
try:
    import agent_tools.{module}
except ImportError as err:
    print(err.name)
"""

_LAKE = ["lake_query", "lake_traces", "lake_sync"]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


@pytest.mark.parametrize("module", [*_LAKE, "run_store", "dash_detail_machine"])
def test_import_loads_no_heavy_dependency(module: str) -> None:
    if module in _LAKE:
        pytest.importorskip("pyiceberg")  # the lake extra; the modules cannot import without it
    out = _run(_CODE.format(module=module))
    assert out.stdout.strip() == "[]", out.stderr


@pytest.mark.parametrize("module", ["lake_traces", "lake_sync"])
def test_a_missing_pyarrow_still_fails_the_import(module: str) -> None:
    pytest.importorskip("pyiceberg")
    out = _run(_BLOCKED.format(module=module))
    assert out.stdout.strip() == "pyarrow", out.stderr


def test_db_errors_is_the_old_tuple() -> None:
    from agent_tools import run_store

    psycopg = pytest.importorskip("psycopg")
    assert run_store._DB_ERRORS == run_store._db_errors() == (run_store.sqlite3.DatabaseError, psycopg.Error)

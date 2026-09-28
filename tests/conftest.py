import re
import sys

import pytest

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _no_cached_core_or_harness_module():
    """`_schema_versions()` does a real `import core` / `import harness`; a
    fake package one test puts on `sys.path` must not leak, via the module
    cache, into a test that runs after it expects the real absence."""
    for name in ("core", "harness"):
        sys.modules.pop(name, None)
    yield
    for name in ("core", "harness"):
        sys.modules.pop(name, None)


def strip_ansi(text: str) -> str:
    """Removes SGR escape sequences, so a help-text assertion reads the same
    whether or not argparse's colorizer is active (Python 3.14 colours help
    output whenever FORCE_COLOR is set, as the cox-launched session does)."""
    return _ANSI.sub("", text)


def pytest_addoption(parser):
    parser.addoption(
        "--regen", action="store_true",
        help="rewrite the help fixtures before the run, instead of only comparing",
    )


def pytest_configure(config):
    # A harness build node may run pytest but not the generators, so this is how a build that
    # changes a command refreshes its snapshots: `pytest --regen`, then the snapshot tests compare as usual.
    if not config.getoption("--regen"):
        return
    import test_cli_help_snapshot

    test_cli_help_snapshot._write()


@pytest.fixture(autouse=True)
def _fresh_store_url():
    """run_store caches the store URL per process; each test starts without it."""
    from agent_tools import console_screen, run_store

    caches = (run_store._store_url_for, run_store._lease_table, run_store._traces_root_for,
              run_store._harness_python_for, run_store._harness_dump, run_store._found_parquet_rows,
              run_store._store_ids_by_trace, console_screen._work_items_snapshot)
    for cache in caches:
        cache.cache_clear()
    yield
    for cache in caches:
        cache.cache_clear()

@pytest.fixture(autouse=True)
def _no_host_profile(monkeypatch, tmp_path_factory):
    """The default profile is not absent in tests: `~/.config/agent-tools/profile.yaml` is read unless a test names another."""
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path_factory.getbasetemp() / "no-profile.yaml"))


@pytest.fixture(autouse=True)
def _no_real_harness(monkeypatch):
    """No test reaches the machine's real harness: a store_cli call would write leases into a real store. A test that
    needs a harness monkeypatches `store_cli._harness_python` itself."""
    from agent_tools import store_cli

    monkeypatch.setattr(store_cli, "_harness_python", lambda: None)

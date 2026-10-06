"""The command table names each handler as `module:function` data and imports it when the command runs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_tools import chair_run, cli, commands, stats_chair, stats_gates

_PROBE = """
import json, sys
import agent_tools.cli as cli
try:
    code = cli.main(%r)
except SystemExit as exc:
    code = exc.code
loaded = sorted(m for m in sys.modules if m.startswith("agent_tools.") and m not in ("agent_tools.cli", "agent_tools.commands"))
print(json.dumps({"code": code, "loaded": loaded}), file=sys.stderr)
"""


def _probe(argv: list[str]) -> tuple[dict, str]:
    """A fresh interpreter's exit code, its loaded agent_tools modules, and its stderr above the probe line."""
    run = subprocess.run([sys.executable, "-c", _PROBE % (argv,)], capture_output=True, text=True)
    *rest, last = run.stderr.strip().splitlines()
    return json.loads(last), "\n".join(rest)


def _rows(table: list[tuple[commands.Group, list[commands.Command]]]) -> list[commands.Command]:
    def walk(row: commands.Command) -> list[commands.Command]:
        return [row, *(r for child in row.subcommands for r in walk(child))]

    return [r for _, rows in table for row in rows for r in walk(row)]


def test_every_real_row_names_its_handler_as_a_module_function_path() -> None:
    handlers = [row.handler for row in _rows(cli.COMMAND_TABLE) if row.handler is not None]
    handlers += [group.fn for group, _ in cli.COMMAND_TABLE if group.fn is not None]
    assert len(handlers) == 100
    outside = [h.target for h in handlers if not h.target.startswith("agent_tools.cli:")]
    assert all(isinstance(h, cli._LazyHandler) for h in handlers)
    assert outside == ["agent_tools.store_fill:main"]  # the one handler whose edge lives in its own module
    assert cli.RUNS_COMMANDS[0].handler == "agent_tools.cli:_runs_usage"


def test_a_real_command_resolves_its_handler_by_path_at_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_runs_usage", lambda a: 41 if a.run_id == "r1" else 0)
    assert cli.main(["runs", "usage", "r1"]) == 41


def test_a_table_entry_naming_a_stub_module_imports_it_only_on_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lazy_stub_cmd.py").write_text(textwrap.dedent("""
        def run(a):
            return 7
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    group = commands.Group("stub", "stub group", "stub group", "")
    row = commands.Command("go", "stub", "run the stub", (), "lazy_stub_cmd:run", False, ())
    [(built_group, built_rows)] = cli._dispatch_table([(group, [row])])
    parser = argparse.ArgumentParser()
    commands.build_parser(built_rows, [built_group], parser.add_subparsers())
    namespace = parser.parse_args(["stub", "go"])
    assert "lazy_stub_cmd" not in sys.modules
    try:
        assert namespace.fn(namespace) == 7
        assert "lazy_stub_cmd" in sys.modules
    finally:
        sys.modules.pop("lazy_stub_cmd", None)


def test_help_imports_no_command_module() -> None:
    result, _ = _probe(["--help"])
    assert result == {"code": 0, "loaded": []}


def test_unknown_command_exits_2_and_imports_no_command_module() -> None:
    result, err = _probe(["no-such-command"])
    assert result == {"code": 2, "loaded": []}
    assert "invalid choice: 'no-such-command'" in err


def test_argument_errors_exit_2_with_argparse_message_and_import_no_command_module() -> None:
    missing, missing_err = _probe(["runs", "usage"])
    bad_choice, bad_choice_err = _probe(["runs", "cause", "r", "t", "bogus"])
    assert missing == {"code": 2, "loaded": []}
    assert "the following arguments are required: run_id" in missing_err
    assert bad_choice == {"code": 2, "loaded": []}
    assert "invalid choice: 'bogus'" in bad_choice_err


def test_lazy_module_forwards_attribute_writes_to_the_real_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.chair_run, "DEFAULT_INTERVAL", 5.0)
    assert chair_run.DEFAULT_INTERVAL == 5.0


def test_build_time_defaults_match_the_constants_they_copy() -> None:
    assert cli._STATS_CAUSES == stats_chair.CAUSES
    assert cli._GATES_MIN_SAMPLE == stats_gates.MIN_SAMPLE
    assert cli._CHAIR_RUN_INTERVAL == chair_run.DEFAULT_INTERVAL

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys

import pytest

from agent_tools.cli import main, version_line

# A fresh interpreter: in-process, other test files have already imported command modules,
# so a sys.modules before/after diff here would be empty whatever --version did.
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
    run = subprocess.run([sys.executable, "-c", _PROBE % (argv,)], capture_output=True, text=True)
    return json.loads(run.stderr.strip().splitlines()[-1]), run.stdout


def test_version_line_formats_a_literal() -> None:
    assert version_line("1.2.3") == "cox 1.2.3"


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_one_line_and_exits_zero(flag: str, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([flag])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"cox {importlib.metadata.version('coxswain-tools')}\n"


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_loads_no_command_module(flag: str) -> None:
    result, out = _probe([flag])
    assert result == {"code": 0, "loaded": []}
    assert out == f"cox {importlib.metadata.version('coxswain-tools')}\n"

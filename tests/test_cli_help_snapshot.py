"""Byte-for-byte `--help` snapshots of the `runs` group and every subcommand.

The fixtures live under tests/fixtures/help/runs/, so the command-table
migration (docs/design/command-table.md) is proven to leave the help output
identical. Regenerate them on purpose only:

    python -m tests.test_cli_help_snapshot --write
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from agent_tools import cli

HELP_ROOT = Path(__file__).resolve().parent / "fixtures" / "help"
GROUPS = ["runs", "courier", "home", "versions", "usage", "plan", "epic", "stats", "lake", "setup", "route", "chair", "dev", "host"]
COLUMNS = "100"  # argparse wraps at the terminal width; pin it so the text is stable


def fixtures_for(group: str) -> Path:
    return HELP_ROOT / group


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Empty for a leaf group (e.g. `home`), which has no subcommands of its own."""
    action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    return dict(action.choices) if action else {}


def help_texts(group: str) -> dict[str, str]:
    """fixture stem -> help text, for the group and each of its subcommands."""
    os.environ["COLUMNS"] = COLUMNS
    top = _subparsers(cli.build_parser())[group]
    texts = {group: top.format_help()}
    for name, sub in _subparsers(top).items():
        texts[f"{group}-{name}"] = sub.format_help()
    return texts


def normalized(text: str) -> str:
    """The usage block (up to the first blank line) has its whitespace runs collapsed; the rest is kept as is."""
    usage, sep, rest = text.partition("\n\n")
    return " ".join(usage.split()) + sep + rest


def _write() -> None:
    for group in GROUPS:
        fixtures = fixtures_for(group)
        fixtures.mkdir(parents=True, exist_ok=True)
        for stem, text in help_texts(group).items():
            (fixtures / f"{stem}.txt").write_text(text)
            print(f"wrote {fixtures / f'{stem}.txt'}")


def _cases() -> list[tuple[str, str]]:
    return sorted(
        (group, p.stem) for group in GROUPS for p in fixtures_for(group).glob("*.txt")
    )


@pytest.mark.parametrize("group,stem", _cases())
def test_help_matches_its_fixture(group: str, stem: str, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", COLUMNS)
    assert normalized(help_texts(group)[stem]) == normalized((fixtures_for(group) / f"{stem}.txt").read_text())


def test_normalized_ignores_usage_line_breaks_but_not_the_body() -> None:
    choices = "{usage,trace,clean,land,review,recover,series,wait,events,top,bar,notify,detail,stranded,cause}"
    py312 = f"usage: cox runs [-h]\n                {choices}\n                ...\n\nbody one\n"
    py314 = f"usage: cox runs [-h]\n                {choices} ...\n\nbody one\n"
    assert normalized(py312) == normalized(py314)
    assert normalized(py312) != normalized(py314.replace("body one", "body two"))


@pytest.mark.parametrize("group", GROUPS)
def test_every_subcommand_has_a_fixture_and_no_fixture_is_stale(group: str, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", COLUMNS)
    assert set(help_texts(group)) == {p.stem for p in fixtures_for(group).glob("*.txt")}


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        _write()
    else:
        print(__doc__)

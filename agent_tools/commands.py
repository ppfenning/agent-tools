"""The command table: `Command` rows and the generator that folds them into
argparse subparsers, per docs/design/command-table.md.

A group's own parser (its help/description/epilog) is not part of a `Command`
row -- rows are leaf subcommands -- so callers also supply one `Group` per
distinct `group` value, carrying that group-level text.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Arg:
    """One `add_argument` call: positional or flag names, plus its kwargs."""

    flags: tuple[str, ...]
    kwargs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Command:
    name: str
    group: str
    summary: str
    args: tuple[Arg, ...]
    handler: Callable[[argparse.Namespace], int] | None
    slash: bool
    examples: tuple[str, ...]
    subcommands: tuple[Command, ...] = ()
    sub_dest: str | None = None
    sub_required: bool = False
    defaults: dict = field(default_factory=dict)
    description: str = ""  # shown by `<group> <name> --help`, one line per statement
    exclusive: tuple[str, ...] = ()  # first flags of `args` that argparse refuses to accept together


@dataclass(frozen=True)
class Group:
    name: str
    help: str
    description: str
    epilog: str
    args: tuple[Arg, ...] = field(default_factory=tuple)
    fn: Callable[[argparse.Namespace], int] | None = None


def _bare_group(parser: argparse.ArgumentParser) -> Callable[[argparse.Namespace], int]:
    """Default `fn` for a group parser whose subcommand is optional: an
    operator who runs the group alone sees that group's own help and a exit
    code of 2, not a traceback or silence."""
    def _fn(_a: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _fn


def _add_row(sub: argparse._SubParsersAction, row: Command) -> None:
    """One subparser for `row`; a row with `subcommands` recurses into its own."""
    formatter = argparse.RawDescriptionHelpFormatter if row.description else argparse.HelpFormatter
    rp = sub.add_parser(row.name, help=row.summary, description=row.description or None, formatter_class=formatter)
    group = rp.add_mutually_exclusive_group() if row.exclusive else rp
    for arg in row.args:
        (group if arg.flags[0] in row.exclusive else rp).add_argument(*arg.flags, **arg.kwargs)
    if row.subcommands:
        rp_sub = rp.add_subparsers(dest=row.sub_dest, required=row.sub_required)
        for child in row.subcommands:
            _add_row(rp_sub, child)
    fn = _bare_group(rp) if row.subcommands and row.handler is None else row.handler
    rp.set_defaults(fn=fn, **row.defaults)


def build_parser(
    commands: list[Command],
    groups: list[Group],
    sub: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    """Fold `commands` into one subparser per row, under one parser per
    `groups` entry, attached to `sub`. A group with no rows is a leaf: it
    takes `Group.args` directly and runs `Group.fn`, no nested subcommand.
    Returns group name -> its parser."""
    by_group: dict[str, list[Command]] = {}
    for row in commands:
        by_group.setdefault(row.group, []).append(row)
    parsers: dict[str, argparse.ArgumentParser] = {}
    for g in groups:
        gp = sub.add_parser(
            g.name, help=g.help, description=g.description, epilog=g.epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        for arg in g.args:
            gp.add_argument(*arg.flags, **arg.kwargs)
        rows = by_group.get(g.name, [])
        if rows:
            gp.set_defaults(fn=_bare_group(gp))
            gp_sub = gp.add_subparsers(dest="cmd", required=False)
            for row in rows:
                _add_row(gp_sub, row)
        else:
            gp.set_defaults(fn=g.fn)
        parsers[g.name] = gp
    return parsers

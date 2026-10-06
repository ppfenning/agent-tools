"""The one rule for handing a retired command's screen to towpath.

`choose_binary` and `decide` are the pure core. `launch` is the edge: it gathers
the facts, asks the core, and either replaces the process with towpath or tells
the caller to print its own --once form.
"""

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Exec:
    path: str


@dataclass(frozen=True)
class Fallback:
    pass


Decision = Exec | Fallback

# Tried in order. The second name is the alias the dashboard shipped under before the rename.
BINARIES = ("towpath", "coxtop")


def choose_binary(found: dict[str, str | None]) -> str | None:
    """The path of the first of BINARIES that PATH lookup found, else None."""
    return next((found[name] for name in BINARIES if found.get(name) is not None), None)


def decide(binary_path: str | None, stdout_is_tty: bool, stdin_is_tty: bool) -> Decision:
    """Exec only when towpath is installed and both streams are a terminal."""
    return Exec(binary_path) if binary_path is not None and stdout_is_tty and stdin_is_tty else Fallback()


def launch(which=shutil.which, execv=os.execv, stdout=sys.stdout, stdin=sys.stdin) -> bool:
    """Exec towpath, or return False so the caller prints its --once form."""
    found = {name: which(name) for name in BINARIES}
    match decide(choose_binary(found), stdout.isatty(), stdin.isatty()):
        case Exec(path=path):
            execv(path, [path])
            # The real os.execv never returns; True is only seen when execv is a stub.
            return True
        case Fallback():
            return False

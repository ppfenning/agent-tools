"""Pure rewrite: widen a ticket's surfaces for named additions and note each one in its body."""

from __future__ import annotations

import re
from collections.abc import Sequence
from itertools import takewhile
from typing import NamedTuple

WIDEN_MARK = "Widened by the chair"

_ITEM = re.compile(r"^(\s*)-\s+(.*)$")
_TOKEN = re.compile(r"""'[^']*'|"[^"]*"|[^,\s][^,]*""")
_CODE = re.compile(r"""(?:'[^']*'|"[^"]*"|[^'"\s]|\s(?!#))*""")
_PLAIN = re.compile(r"[^,\[\]#\s]+(?:[^,\[\]#]*[^,\[\]#\s])?")


class _Surfaces(NamedTuple):
    start: int
    end: int
    flow: bool
    tokens: list[str]
    paths: list[str]
    comment: str
    indent: str


def _code(value: str) -> str:
    """`value` up to a ` #` comment that sits outside quotes."""
    match = _CODE.match(value)
    return match.group().rstrip() if match else value.rstrip()


def _unquote(value: str) -> str:
    return value.strip().strip("'\"")


def _token(path: str) -> str:
    return path if _PLAIN.fullmatch(path) else f"'{path}'"


def _split(text: str) -> tuple[list[str], str] | None:
    """The `---` header lines (opening fence included) and the body; `None` without a closed header."""
    found = text.find("\n---\n", 4) if text.startswith("---\n") else -1
    close = len(text) - 4 if found == -1 and text.startswith("---\n") and text.endswith("\n---") else found
    return None if close < 4 else (text[:close].split("\n"), text[close + len("\n---\n"):])


def _flow(header: list[str], start: int) -> _Surfaces | None:
    """A `[...]` list opening on line `start`, possibly wrapped over later lines."""
    end = next((k for k in range(start, len(header)) if "]" in _code(header[k])), None)
    if end is None:
        return None
    joined = " ".join(_code(line) for line in header[start:end + 1])
    if not joined.endswith("]"):
        return None
    tokens = [t.strip() for t in _TOKEN.findall(joined[joined.index("[") + 1:-1])]
    comment = header[end][len(_code(header[end])):]
    return _Surfaces(start, end + 1, True, tokens, [_unquote(t) for t in tokens], comment, "")


def _read_surfaces(header: list[str]) -> _Surfaces | str:
    """The surfaces list, flow or block; else a refusal note saying why it cannot be read."""
    start = next((i for i, line in enumerate(header) if line.startswith("surfaces:")), None)
    if start is None:
        return "widen: work item has no surfaces key"
    value = _code(header[start][len("surfaces:"):]).strip()
    if value.startswith("["):
        flow = _flow(header, start)
        return flow if flow is not None else "widen: work item surfaces is an unclosed flow list"
    if value:
        return f"widen: work item surfaces is {value!r}, not a list"
    items = [m for m in map(_ITEM.match, takewhile(_ITEM.match, header[start + 1:])) if m]
    indent = items[0].group(1) if items else "  "
    paths = [_unquote(_code(m.group(2))) for m in items]
    return _Surfaces(start, start + 1 + len(items), False, [], paths, "", indent)


def _write_surfaces(header: list[str], s: _Surfaces, new: list[str]) -> list[str]:
    """`header` with `new` paths appended to the surfaces list, in the style it was written in."""
    if s.flow:
        line = f"surfaces: [{', '.join(s.tokens + [_token(p) for p in new])}]{s.comment}"
        return header[:s.start] + [line] + header[s.end:]
    return header[:s.end] + [f"{s.indent}- {_token(p)}" for p in new] + header[s.end:]


def _note(when: str, path: str, addition: str) -> str:
    return (f"{WIDEN_MARK} {when}: {path} is now in surfaces for {addition} only. "
            "Add that one named addition and change nothing else in the file.")


def _bad_addition(additions: Sequence[tuple[str, str]]) -> str | None:
    """A refusal note when there is nothing to widen, or a pair would break the one-line note or the list."""
    bad = next(((p, a) for p, a in additions
                if not p.strip() or not a.strip() or any(c in p for c in "\r\n'\"") or any(c in a for c in "\r\n")), None)
    if not additions:
        return "widen: no additions named, nothing to widen"
    return None if bad is None else f"widen: addition {bad!r} is empty, multi-line, or a quoted path"


def widen_ticket_text(text: str, additions: Sequence[tuple[str, str]], when: str) -> tuple[str | None, str | None]:
    """`(new_text, None)` with new paths in surfaces, state ready and one note per named path; else `(None, note)`.

    Only a `ready` or `blocked` ticket is widened, so a done or dropped one is never reopened."""
    parts = _split(text)
    if parts is None:
        return None, "widen: work item has no frontmatter"
    refused = _bad_addition(additions)
    if refused is not None:
        return None, refused
    header, body = parts
    surfaces = _read_surfaces(header)
    if isinstance(surfaces, str):
        return None, surfaces
    state_at = next((i for i, line in enumerate(header) if line.startswith("state:")), None)
    if state_at is None:
        return None, "widen: work item has no state field"
    state = _unquote(_code(header[state_at][len("state:"):]))
    if state not in ("ready", "blocked"):
        return None, f"widen: work item state is {state!r}, not widening"
    first = dict(reversed(additions))  # the first pair named for a path wins
    order = list(dict.fromkeys(p for p, _ in additions))
    widened = _write_surfaces(header, surfaces, [p for p in order if p not in surfaces.paths])
    state_line = next(i for i, line in enumerate(widened) if line.startswith("state:"))
    state_comment = widened[state_line][len(_code(widened[state_line])):]
    readied = widened[:state_line] + [f"state: ready{state_comment}"] + widened[state_line + 1:]
    notes = "\n\n".join(_note(when, p, first[p]) for p in order)
    # A changed body resets the attempts counted on the current body, so the ticket is retried like a fresh one.
    new_body = (body.rstrip("\n") + "\n\n" if body.strip() else "") + notes + "\n"
    return "\n".join(readied) + "\n---\n" + new_body, None


def widening_count(text: str) -> int:
    """How many body lines start with `WIDEN_MARK`; a text with no closed header counts as all body."""
    parts = _split(text)
    body = text if parts is None else parts[1]
    return sum(1 for line in body.splitlines() if line.startswith(WIDEN_MARK))

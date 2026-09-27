"""The `quarantined` reader for `chair_facts.FactsDeps`: open quarantined work items as rows.

A quarantine is a ready or blocked item with an attempt on its current body. The driver leaves the item's
state alone and appends the attempt to its frontmatter; no state is ever `quarantined`."""
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from agent_tools import route, run_store
from agent_tools.stats_chair import frontmatter_item

OPEN_STATES = ("ready", "blocked")

Row = dict[str, str]
Attempt = Mapping[str, object]
Item = tuple[str, str, str, Sequence[Attempt] | None]


def _row(path: str) -> Row | None:
    """Only `work/<initiative>/<phase>/<task>.md` has a row; the task is the file stem."""
    p = PurePosixPath(path)
    if len(p.parts) == 4 and p.parts[0] == "work" and p.suffix == ".md":
        return {"initiative": p.parts[1], "phase": p.parts[2], "task": p.stem}
    return None


def body_sha(body: str) -> str:
    """The rule cartridges uses to name a work item's body: 12 hex digits of the stripped body's sha256."""
    return hashlib.sha256(body.strip().encode()).hexdigest()[:12]


def attempts_on_current_body(attempts: Iterable[Attempt], body: str) -> list[Attempt]:
    """The attempts made on `body`, plus those that recorded no `body_sha`."""
    sha = body_sha(body)
    return [a for a in attempts if not a.get("body_sha") or a.get("body_sha") == sha]


def item_body(text: str) -> str:
    """The text after the closing `---` of the frontmatter; the whole text when it has none."""
    close = text.find("\n---", 4) if text.startswith("---\n") else -1
    return text[close + 4 :] if close != -1 else text


def quarantined_rows(items: Iterable[Item]) -> list[Row]:
    """Rows for (path, state, body, attempts) items in state ready or blocked with an attempt on the current body, in input order.

    The row carries `run` from the newest such attempt by `ts`."""
    rows = []
    for path, state, body, attempts in items:
        on_body = attempts_on_current_body([a for a in attempts or [] if isinstance(a, Mapping)], body)
        row = _row(path) if state in OPEN_STATES and on_body else None
        if row is not None:
            newest = max(on_body, key=lambda a: str(a.get("ts") or ""))
            rows.append({**row, "run": str(newest.get("run") or "")})
    return rows


def quarantined_from_rows(rows: Iterable[Mapping[str, object]]) -> list[Row]:
    """Quarantine facts for rows whose `state` is "quarantined": one row per queue row, in input order.

    A row already names its own quarantine, so no state or attempt-matching gate is needed to find one; the
    newest attempt in `extra["attempts"]` on the row's current `body` supplies `run` and `cause`, by the same
    `body_sha`/`attempts_on_current_body` rule `quarantined_rows` uses. A row with no such attempt still gets
    a fact, with `run` and `cause` empty."""
    facts = []
    for row in rows:
        if row.get("state") != "quarantined":
            continue
        body = str(row.get("body") or "")
        attempts = [a for a in (row.get("extra") or {}).get("attempts") or [] if isinstance(a, Mapping)]
        on_body = attempts_on_current_body(attempts, body)
        newest = max(on_body, key=lambda a: str(a.get("ts") or "")) if on_body else {}
        facts.append(
            {
                "initiative": str(row.get("initiative") or ""),
                "phase": str(row.get("phase") or ""),
                "task": str(row.get("task_id") or ""),
                "run": str(newest.get("run") or ""),
                "body_sha": body_sha(body),
                "cause": str(newest.get("cause") or ""),
            }
        )
    return facts


def read_quarantined(root: Path, mode: str) -> list[Row]:
    """Edge. The store is read, and overrides file state, only under mode "store", as `cli._stored_work_items` does."""
    texts = {p: p.read_text() for p in sorted(root.glob("work/*/*/*.md"))}
    items = [
        route.work_item(route.parse_frontmatter(text)[0], initiative=p.parts[-3], phase_dir=p.parts[-2], stem=p.stem)
        for p, text in texts.items()
    ]
    stored = route.with_store_states(items, run_store.work_items(root / "runs") if mode == "store" else [], mode)
    return quarantined_rows(
        (
            f"work/{item['initiative']}/{item['file']}",
            item["state"],
            item_body(text),
            frontmatter_item(text, p.stem).get("attempts"),
        )
        for (p, text), item in zip(texts.items(), stored, strict=True)
    )

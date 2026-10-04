"""Courier reference: parse, format, and resolve `coxswain://<kind>/<id>` (docs/design/courier.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from agent_tools.route import intake_entries, parse_frontmatter
from agent_tools.stats_ingest import LEDGER_PATH, _read_ledger

__all__ = ["Reference", "ack", "ack_file", "append_line", "format_reference", "inbox", "parse_reference", "resolve", "send"]

_KINDS = ("run", "task", "pr", "intake", "proposal", "finding", "initiative")
_PATTERN = re.compile(r"^coxswain://([a-z]+)/(.+)$")


class Reference(NamedTuple):
    kind: str
    id: str


def parse_reference(s: str) -> Reference | None:
    match = _PATTERN.match(s)
    if match is None:
        return None
    kind, id_ = match.group(1), match.group(2)
    return Reference(kind, id_) if kind in _KINDS else None


def format_reference(ref: Reference) -> str:
    return f"coxswain://{ref.kind}/{ref.id}"


def _run_record(runs_dir: Path, run_id: str) -> dict | None:
    matches = sorted((runs_dir / run_id / "tasks").glob("*/*.json"))
    if not matches:
        return None
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _finding(record: dict) -> dict | None:
    """`arbitration.reasoning`, falling back to `adversary`'s `why_wrong` (the fields `runs_detail._objection` reads)."""
    arbitration = record.get("arbitration")
    reasoning = arbitration.get("reasoning") if isinstance(arbitration, dict) else None
    if reasoning:
        return {"reasoning": reasoning}
    adversary = record.get("adversary") or []
    findings = adversary if isinstance(adversary, list) else [adversary]
    for finding in findings:
        why_wrong = (finding or {}).get("why_wrong") if isinstance(finding, dict) else None
        if why_wrong:
            return {"reasoning": why_wrong}
    return None


def _ledger_row(key: str, ledger_path: Path) -> dict | None:
    return next((row for row in _read_ledger(ledger_path, []) if row.get("key") == key), None)


def _work_item(workspace_dir: Path, rel_path: str) -> dict | None:
    try:
        text = (workspace_dir / rel_path).read_text(encoding="utf-8")
    except OSError:
        return None
    fields, body = parse_frontmatter(text)
    return {**fields, "body": body} if fields else None


def _find_by_id(workspace_dir: Path, glob_pattern: str, ref_id: str) -> Path | None:
    for path in sorted(workspace_dir.glob(glob_pattern)):
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fields and fields.get("id") == ref_id:
            return path
    return None


def _resolve_by_search(workspace_dir: Path, ref_id: str, direct: Path | None, glob_pattern: str) -> dict | None:
    """`task`/`initiative`: a path-shaped `ref_id` resolves as today; otherwise search by frontmatter `id`
    and stamp the found record with `path`, the workspace-relative path, for `cox courier inbox` to print."""
    if (workspace_dir / ref_id).exists():
        return _work_item(workspace_dir, ref_id)
    path = direct if direct and direct.exists() else _find_by_id(workspace_dir, glob_pattern, ref_id)
    if path is None:
        return None
    rel = path.relative_to(workspace_dir).as_posix()
    item = _work_item(workspace_dir, rel)
    return {**item, "path": rel} if item else None


def _intake_record(workspace_dir: Path, ticket_id: str) -> dict | None:
    """`route.intake_entries`, over every file under `intake/`, filtered to the entry whose own `id` matches —
    the ticket id courier.md fixes as `intake`'s id, distinct from a task's path."""
    intake_dir = workspace_dir / "intake"
    if not intake_dir.is_dir():
        return None
    files = {p.relative_to(intake_dir).as_posix(): p.read_text(encoding="utf-8") for p in intake_dir.rglob("*.md")}
    entry = next((e for e in intake_entries(files) if e["id"] == ticket_id), None)
    return _work_item(workspace_dir, entry["path"]) if entry else None


def resolve(ref: Reference, workspace_dir: Path | str, ledger_path: Path | str | None = None) -> dict | None:
    workspace_dir = Path(workspace_dir)
    runs_dir = workspace_dir / "runs"
    if ref.kind == "run":
        return _run_record(runs_dir, ref.id)
    if ref.kind == "finding":
        record = _run_record(runs_dir, ref.id)
        return _finding(record) if record else None
    if ref.kind in ("pr", "proposal"):
        return _ledger_row(ref.id, Path(ledger_path) if ledger_path is not None else LEDGER_PATH)
    if ref.kind == "intake":
        return _intake_record(workspace_dir, ref.id)
    if ref.kind == "task":
        return _resolve_by_search(workspace_dir, ref.id, None, f"work/*/*/{ref.id}.md")
    if ref.kind == "initiative":
        direct = workspace_dir / "work" / ref.id / "initiative.md"
        return _resolve_by_search(workspace_dir, ref.id, direct, "work/*/initiative.md")
    return _work_item(workspace_dir, ref.id)


def send(ref: Reference, sender: str, to: str, note: str, message_id: str) -> dict:
    """Builds one bus entry; never writes it (docs/design/courier.md `#the-bus`)."""
    return {"ref": format_reference(ref), "from": sender, "to": to, "note": note, "id": message_id, "ack": False}


def append_line(blob: str, entry: dict) -> str:
    return blob + json.dumps(entry) + "\n"


def _latest_by_id(blob: str) -> dict[str, dict]:
    """Last line per id wins, in first-seen order."""
    latest: dict[str, dict] = {}
    for line in blob.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "id" in entry:
            latest[entry["id"]] = entry
    return latest


def inbox(blob: str, label: str | None = None, holder: str | None = None) -> list[dict]:
    """`holder`: the label currently holding the leader lock, read at the edge.
    An entry addressed to the generic `chair` reaches `label` when `label` is the
    holder or `label` itself starts with `chair`; any other `to` needs an exact match."""
    entries = _latest_by_id(blob).values()

    def _matches(e: dict) -> bool:
        if label is None or e["to"] == label:
            return True
        return e["to"] == "chair" and (label == holder or label.startswith("chair"))

    return [e for e in entries if not e["ack"] and _matches(e)]


def ack(blob: str, message_id: str) -> str:
    entry = _latest_by_id(blob).get(message_id)
    return blob if entry is None else append_line(blob, {**entry, "ack": True})


def ack_file(path: Path, message_id: str) -> bool:
    """Appends one ack line and never rewrites the file; False when no entry has `message_id`.
    A final line missing its newline (a torn write) is closed first so the ack stays on its own line."""
    try:
        blob = path.read_text(encoding="utf-8")
    except OSError:
        return False
    entry = _latest_by_id(blob).get(message_id)
    if entry is None:
        return False
    prefix = "\n" if blob and not blob.endswith("\n") else ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + json.dumps({**entry, "ack": True}) + "\n")
    return True


def entries(blob: str) -> list[dict]:
    """Every entry once, in first-seen order, at its latest state."""
    return list(_latest_by_id(blob).values())

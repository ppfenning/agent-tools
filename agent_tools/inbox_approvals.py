"""Inbox source: draft initiatives awaiting `cox route approve` or `cox route decline`."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import route
from agent_tools.inbox import InboxItem, item_id

_DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"  # mirrors cli.DEFAULT_PROFILE; cli.py is not imported here
_DECLINE_REASON = "declined from the inbox"  # `cox route decline` requires --reason
_UNDATED = datetime.min.replace(tzinfo=UTC)  # no proposed_at: sorts oldest, so it is never buried


def _created_at(proposed_at: object) -> datetime:
    if not isinstance(proposed_at, str):
        return _UNDATED
    try:
        parsed = datetime.fromisoformat(proposed_at)
    except ValueError:
        return _UNDATED
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def approvals_to_items(records: Sequence[Mapping]) -> tuple[InboxItem, ...]:
    """Records carry `id`, `proposed_at` (ISO) and `reasoning`; each becomes one approval item."""
    return tuple(
        InboxItem(
            id=item_id("approval", str(r["id"])),
            kind="approval",
            created_at=_created_at(r.get("proposed_at")),
            what=f"initiative {r['id']} awaits routing approval",
            evidence=str(r.get("reasoning") or ""),
            accept_cmd=("cox", "route", "approve", str(r["id"])),
            deny_cmd=("cox", "route", "decline", str(r["id"]), "--reason", _DECLINE_REASON),
        )
        for r in records
    )


def _is_draft(value: object) -> bool:
    return value is True or value == "true"


def _record(path: Path) -> dict | None:
    """The pending-approval record of one `initiative.md`; None when unreadable or not a draft."""
    try:
        fields = route.parse_frontmatter(path.read_text(encoding="utf-8"))[0]
    except (OSError, UnicodeDecodeError):
        return None
    if not _is_draft(fields.get("draft")):
        return None
    evidence = fields.get("evidence") or ""
    return {
        "id": fields.get("id") or path.parent.name,
        "proposed_at": fields.get("proposed_at"),
        "reasoning": evidence if isinstance(evidence, str) else "; ".join(evidence),
    }


def load_approval_records() -> list[dict]:
    """Edge. Draft initiatives under `<workspace_dir>/work`, the set `cox route approve` acts on; empty with no profile."""
    profile_path = Path(os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE).expanduser()
    try:
        workspace = route.parse_profile(profile_path.read_text(encoding="utf-8")).get("workspace_dir", "")
    except (OSError, UnicodeDecodeError, route.ProfileError):
        return []
    if not workspace:
        return []
    found = (_record(p) for p in sorted((Path(workspace).expanduser() / "work").glob("*/initiative.md")))
    return [r for r in found if r is not None]

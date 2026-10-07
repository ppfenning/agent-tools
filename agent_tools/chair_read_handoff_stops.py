"""The `handoff_stops` reader for `chair_facts.FactsDeps`: open tickets stopped on an attempt, with the newest reason.

It does not judge whether a stop is a handoff or a small addition. The planner's rule does that."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_tools.chair_read_quarantined import (
    OPEN_STATES,
    attempts_on_current_body,
    item_body,
    newest_reason,
    read_work_items,
)
from agent_tools.chair_types import HandoffStop
from agent_tools.chair_widen_ticket import widening_count
from agent_tools.stats_chair import frontmatter_item


def handoff_stops_from(items: Iterable[Mapping]) -> list[HandoffStop]:
    """One row per ready or blocked item with an attempt on its current body, in input order.

    `reason` is the newest such attempt's. `surfaces` keeps each string as written, '(new)' suffix included."""
    rows = []
    for item in items:
        body = str(item.get("body") or "")
        attempts = [a for a in item.get("attempts") or [] if isinstance(a, Mapping)]
        on_body = attempts_on_current_body(attempts, body)
        raw = item.get("surfaces")
        if item.get("state") in OPEN_STATES and on_body:
            rows.append(
                {
                    "initiative": str(item.get("initiative") or ""),
                    "task_id": str(item.get("task_id") or ""),
                    "state": str(item.get("state")),
                    "reason": newest_reason(on_body),
                    "surfaces": [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else [],
                    "widenings": widening_count(body),
                }
            )
    return rows


def _item(path: Path, text: str, work_item: Mapping) -> dict | None:
    """The core's input for one ticket; None for one that cannot be read."""
    try:
        attempts = frontmatter_item(text, path.stem).get("attempts")
        return {
            "initiative": work_item["initiative"],
            "task_id": work_item["id"],
            "state": work_item["state"],
            "body": item_body(text),
            "surfaces": work_item.get("surfaces"),
            "attempts": attempts if isinstance(attempts, list) else [],
        }
    except Exception:
        return None


def read_handoff_stops(root: Path, mode: str) -> list[HandoffStop]:
    """Edge. The handoff stops among `read_work_items(root, mode)`; a ticket that cannot be read is skipped and nothing raises."""
    try:
        listed = read_work_items(root, mode)
    except Exception:
        return []
    return handoff_stops_from(i for p, text, item in listed if (i := _item(p, text, item)) is not None)

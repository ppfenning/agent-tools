"""The rule for one intake entry's queue state: `queued`, `decomposed`, or `landed`.

Pure core, no I/O. `route.intake_groups` calls `intake_group` per entry.
"""

from __future__ import annotations

import re

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)
_INTAKE_LINE = re.compile(r"^intake:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def intake_field(text: str) -> str | None:
    """The first `intake:` line of the leading frontmatter block, quotes stripped; None with no block or no line."""
    block = _FRONTMATTER.match(text)
    line = _INTAKE_LINE.search(block.group(1)) if block else None
    return line.group(1).strip("\"'") if line else None


def intake_group(entry: dict, initiatives_by_id: dict, initiatives: list) -> str:
    initiative_id = entry.get("initiative")
    if initiative_id is not None:
        initiative = initiatives_by_id.get(initiative_id)
        return "landed" if initiative and initiative["done"] else "decomposed"
    naming = [i for i in initiatives if intake_field(i.get("text", "")) == entry["path"]]
    if naming:
        return "landed" if all(i["done"] for i in naming) else "decomposed"
    if not entry.get("done"):
        return "queued"
    cited = any(entry["path"] in i.get("text", "") for i in initiatives)
    return "decomposed" if cited else "landed"

"""Models and tiers rows built from the cartridge's model map.

Pure: takes the already-parsed model map, returns plain data in the shape of
`cox_settings._row_json`. No file, git, clock or environment access.

unknown: the model map's shape was not found in existing code. Assumed: a
mapping of role to a mapping with an optional `model` and an optional `tier`.
unknown: the row key naming. Assumed: `models.<role>.model` and
`models.<role>.tier`.

`policy.plan_competition.min_tier` is not built here; the wiring task keeps it.
"""

from __future__ import annotations

from agent_tools.settings_model import CARTRIDGE_FILE

SECTION = "models and tiers"
FIELDS = ("model", "tier")


def _row(key: str, value: object, tracked: bool) -> dict:
    return {
        "section": SECTION,
        "scope": "cartridge",
        "key": key,
        "value": value,
        "source_file": CARTRIDGE_FILE,
        "tracked": tracked,
        "pat_only": True,
    }


def model_tier_rows(model_map: dict, tracked: bool = False) -> list[dict]:
    """Per role in map order, a `model` row then a `tier` row; a field the role lacks yields no row."""
    return [
        _row(f"models.{role}.{field}", entry[field], tracked)
        for role, entry in model_map.items()
        if isinstance(entry, dict)
        for field in FIELDS
        if entry.get(field) is not None
    ]

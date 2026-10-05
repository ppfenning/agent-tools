"""Builds and budgets rows from a parsed cartridge.

Pure: takes the already-parsed cartridge dict, the cartridge file name the caller
reads it from, and the tracked map; returns plain data. No file, git, clock or
environment access, and no default row for a key the cartridge does not carry.

Keys carried, in row order, all cartridge scope, each with where the repo names it:

- `policy.build_budget_usd_max`, the per-run build budget ceiling.
  `agent_tools/provenance.py:17` lists it in `_FIXED_PATHS` and
  `agent_tools/settings_model.py:93` registers it under builds and budgets.
- `epic_threshold`, the size bar above which work becomes an epic.
  `docs/superpowers/specs/2026-09-03-route-layer-design.md:166` reads
  "Size against the cartridge's `epic_threshold`." and
  `docs/design/work-shape.md:121` names it. It is read as a top-level key. No
  file in the repo nests it under `policy`, so the nesting is the one fact the
  repo states only by omission.
- `landing_areas.checks`, the checks the land step runs.
  `agent_tools/provenance.py:18` lists it in `_FIXED_PATHS` and
  `tests/test_editor_model.py:71` builds it as `"landing_areas": {"checks": ["ci"]}`.

Left out on purpose: `policy.pacing.*` keys such as `hard_stop_fraction` belong
to the spend and pacing section (`settings_model.py:90-92`), and the spend
ceilings are profile keys. No other cartridge budget, ceiling, land or check
path appears in the repo: `landing_areas.` has no sibling key and `repo_facts`
in `land.py:102-112` reads only `checks`, `venv_python` and `uv_lock`.
"""

from __future__ import annotations

from agent_tools.editor_model import _MISSING, _get
from agent_tools.settings_model import SettingRow

SECTION = "builds and budgets"

BUILDS_KEYS: tuple[str, ...] = (
    "policy.build_budget_usd_max",
    "epic_threshold",
    "landing_areas.checks",
)


def builds_rows(cartridge: dict, source_file: str, tracked: dict[str, bool]) -> list[SettingRow]:
    """Rows for `BUILDS_KEYS` in order; a key absent from the cartridge yields no row."""
    found = [(key, _get(cartridge, key)) for key in BUILDS_KEYS]
    return [
        SettingRow(SECTION, "cartridge", key, value, source_file, tracked.get(source_file, False))
        for key, value in found
        if value is not _MISSING
    ]

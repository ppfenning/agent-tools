"""Built-in defaults as default rows for keys a settings get left unset.

Pure: takes plain rows, returns plain rows. No file, git, clock or environment access.

Where each default comes from, measured in this repository:

- `chair.stale_days`: imported from `agent_tools.chair_facts.DEFAULT_STALE_DAYS`.
- `chair.housekeeping_hours`: imported from `agent_tools.chair_facts.DEFAULT_HOUSEKEEPING_HOURS`.
- `policy.build_budget_usd_max`, `epic_threshold`, `policy.review_tier`: no code default.
  The cartridge sets them. Per the chair pin of 2026-10-05 they stay in the table with
  default `None` and the note "set by the cartridge". No value is invented.

unknown: no per-role model key exists in the registry. `policy.review_tier` is the one
registered models-and-tiers key, so it carries the `pat_only` flag the ticket asks of
role models.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_tools.chair_facts import DEFAULT_HOUSEKEEPING_HOURS, DEFAULT_STALE_DAYS
from agent_tools.settings_model import SettingRow

BUILT_IN = "built-in"
CARTRIDGE_NOTE = "set by the cartridge"


@dataclass(frozen=True)
class DefaultKey:
    scope: str
    key: str
    section: str
    default: object
    pat_only: bool = False


DEFAULT_KEYS: tuple[DefaultKey, ...] = (
    DefaultKey("cartridge", "policy.build_budget_usd_max", "builds and budgets", None),
    DefaultKey("cartridge", "epic_threshold", "builds and budgets", None),
    DefaultKey("cartridge", "policy.review_tier", "models and tiers", None, pat_only=True),
    DefaultKey("profile", "chair.housekeeping_hours", "housekeeping", DEFAULT_HOUSEKEEPING_HOURS),
    DefaultKey("profile", "chair.stale_days", "housekeeping", DEFAULT_STALE_DAYS),
)


def _identity(row: dict | SettingRow) -> tuple[str, str]:
    return (row["scope"], row["key"]) if isinstance(row, dict) else (row.scope, row.key)


def _default_row(spec: DefaultKey) -> dict:
    base = {
        "section": spec.section,
        "scope": spec.scope,
        "key": spec.key,
        "value": spec.default,
        "source_file": BUILT_IN,
        "tracked": False,
        "pat_only": spec.pat_only,
    }
    return {**base, "note": CARTRIDGE_NOTE} if spec.default is None else base


def default_rows(rows: list[dict | SettingRow]) -> list[dict]:
    """One built-in row per `DEFAULT_KEYS` entry whose (scope, key) is absent from `rows`, in table order."""
    present = {_identity(row) for row in rows}
    return [_default_row(spec) for spec in DEFAULT_KEYS if (spec.scope, spec.key) not in present]

"""Settings rows: a registry of named settings, and the rows built from parsed files.

Pure: takes already-parsed cartridge and profile dicts plus a map of source file
to tracked, returns plain data. No file, git, clock or environment access.

Naming, stable because the set task resolves against `REGISTRY` through
`setting_for`:

- scope is `cartridge` or `profile`, the document a setting lives in.
- key is the dotted path inside that document. `scope` and `key` together
  identify one setting, written `scope:key`, e.g. `cartridge:policy.review_tier`.
- source_file is the logical name `cartridge.yaml` or `profile.yaml`. The
  caller's `tracked` map is keyed by those two names. A name missing from the
  map reads as machine-local.
- crew seat keys are `crew.<seat>.enabled` and `crew.<seat>.skills`. They are
  derived from the cartridge, not listed in `REGISTRY`.

Keys by section, in section order:

- lanes and machines: cartridge `policy.dispatch.max_in_flight`,
  `policy.dispatch.local_lanes`.
- spend and pacing: profile `window_ceiling_usd`, `weekly_ceiling_usd`,
  `node_cap_usd`, `weekly_reset`; cartridge `policy.pacing.hard_stop_fraction`,
  `policy.pacing.weekly_hard_stop_fraction`, `policy.pacing.min_elapsed_fraction`.
- builds and budgets: cartridge `policy.build_budget_usd_max`, `epic_threshold`.
- models and tiers (all pat_only): cartridge `policy.review_tier`,
  `policy.plan_competition.min_tier`, `policy.pacing.tier_ladder`,
  `policy.pacing.effort_ladder`.
- crew seats: cartridge `crew.<seat>.enabled`, `crew.<seat>.skills`.
- housekeeping: profile `log_retention_days`, `chair.housekeeping_hours`,
  `chair.stale_days`, `analytics.snapshot_max_age_s`.
- profile files: profile `team`, `cartridges_dir`, `skills_roots`,
  `provider_profile`, `harness_dir`, `workspace_dir`, `assume`, `router`,
  `forge`, `tracker`, `umbrella_dir`, `ids`.

unknown: `lane_hosts` is not in the parsed profile dict, so no row carries it.
`landing_areas.checks`, `context`, `sources` and `repo_map` are not scalar
settings and are not registered.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_tools.editor_model import _MISSING, _crew, _get, _seat_field_paths

SECTIONS = (
    "lanes and machines",
    "spend and pacing",
    "builds and budgets",
    "models and tiers",
    "crew seats",
    "housekeeping",
    "profile files",
)

CARTRIDGE_FILE = "cartridge.yaml"
PROFILE_FILE = "profile.yaml"


@dataclass(frozen=True)
class Setting:
    scope: str
    key: str
    section: str
    source_file: str
    path: str
    pat_only: bool = False


@dataclass(frozen=True)
class SettingRow:
    section: str
    scope: str
    key: str
    value: object
    source_file: str
    tracked: bool


def _in(section: str, scope: str, source_file: str, keys: tuple[str, ...], pat_only: bool = False) -> tuple[Setting, ...]:
    return tuple(Setting(scope, key, section, source_file, key, pat_only) for key in keys)


REGISTRY: tuple[Setting, ...] = (
    *_in("lanes and machines", "cartridge", CARTRIDGE_FILE,
         ("policy.dispatch.max_in_flight", "policy.dispatch.local_lanes")),
    *_in("spend and pacing", "profile", PROFILE_FILE,
         ("window_ceiling_usd", "weekly_ceiling_usd", "node_cap_usd", "weekly_reset")),
    *_in("spend and pacing", "cartridge", CARTRIDGE_FILE,
         ("policy.pacing.hard_stop_fraction", "policy.pacing.weekly_hard_stop_fraction",
          "policy.pacing.min_elapsed_fraction")),
    *_in("builds and budgets", "cartridge", CARTRIDGE_FILE, ("policy.build_budget_usd_max", "epic_threshold")),
    *_in("models and tiers", "cartridge", CARTRIDGE_FILE,
         ("policy.review_tier", "policy.plan_competition.min_tier",
          "policy.pacing.tier_ladder", "policy.pacing.effort_ladder"), pat_only=True),
    *_in("housekeeping", "profile", PROFILE_FILE,
         ("log_retention_days", "chair.housekeeping_hours", "chair.stale_days", "analytics.snapshot_max_age_s")),
    *_in("profile files", "profile", PROFILE_FILE,
         ("team", "cartridges_dir", "skills_roots", "provider_profile", "harness_dir",
          "workspace_dir", "assume", "router", "forge", "tracker", "umbrella_dir", "ids")),
)


def _seat_setting(path: str) -> Setting:
    return Setting("cartridge", path, "crew seats", CARTRIDGE_FILE, path)


def _seat_settings(cartridge: dict) -> tuple[Setting, ...]:
    return tuple(
        _seat_setting(path)
        for seat, value in _crew(cartridge).items()
        for path in _seat_field_paths(cartridge, seat, value)
    )


def setting_for(scope: str, key: str) -> Setting | None:
    """The registered setting for `scope` and `key`; any `cartridge` key under `crew.` resolves as a crew seat."""
    found = [s for s in REGISTRY if (s.scope, s.key) == (scope, key)]
    if found:
        return found[0]
    return _seat_setting(key) if scope == "cartridge" and key.startswith("crew.") else None


def rows(cartridge: dict, profile: dict, tracked: dict[str, bool]) -> dict[str, list[SettingRow]]:
    """Rows grouped by section in `SECTIONS` order; a setting absent from its document yields no row."""
    docs = {"cartridge": cartridge, "profile": profile}
    found = [(s, _get(docs[s.scope], s.path)) for s in (*REGISTRY, *_seat_settings(cartridge))]
    return {
        section: [
            SettingRow(s.section, s.scope, s.key, value, s.source_file, tracked.get(s.source_file, False))
            for s, value in found
            if s.section == section and value is not _MISSING
        ]
        for section in SECTIONS
    }

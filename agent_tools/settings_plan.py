"""Set planner: one setting change as new file text, a unified diff, and the pat_only flag.

Pure: takes scope, key, the raw value string, the current text of the target file
and a validator, returns a `Plan` or a `PlanError`. Nothing is read, written or raised.

The cartridge schema lives in `core.cartridge.overlay_errors`, which only the
harness venv can import. `editor_model` exposes no validator, so the caller passes
one in: `validate(text) -> list of error strings`. Only the `cartridge` scope is
validated; no profile schema exists.

Rewriting goes through `yaml.safe_dump`, so comments and blank lines in the old
text do not survive and the diff shows them as changes.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass

import yaml

# Private names, imported the way settings_model.py imports them.
from agent_tools.editor_model import _MISSING, _get, _set_nested
from agent_tools.settings_model import setting_for


@dataclass(frozen=True)
class Plan:
    new_text: str
    diff: str
    pat_only: bool


@dataclass(frozen=True)
class PlanError:
    reason: str


def _scalar(raw: str) -> object | PlanError:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return PlanError(f"not a valid value: {raw!r}")


def _coerce(current: object, raw: str) -> object | PlanError:
    """`raw` in the type of `current`; with no current value it is parsed as a YAML scalar."""
    kind = type(current)
    if current is _MISSING or current is None:
        return _scalar(raw)
    if kind in (bool, list, dict):
        parsed = _scalar(raw)
        if isinstance(parsed, (PlanError, kind)):
            return parsed
        return PlanError(f"expected {kind.__name__}, got {raw!r}")
    try:
        return kind(raw)
    except (TypeError, ValueError):
        return PlanError(f"expected {kind.__name__}, got {raw!r}")


def _load(text: str) -> dict | PlanError:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return PlanError(f"current file does not parse: {exc}")
    if doc is None:
        return {}
    return doc if isinstance(doc, dict) else PlanError("current file is not a mapping")


def _parts(doc: dict, path: str) -> list[str]:
    """Path segments; `crew` addresses `cast` when the document uses that older name."""
    head, *rest = path.split(".")
    return ["cast" if head == "crew" and "crew" not in doc and "cast" in doc else head, *rest]


def _blocked(doc: dict, parts: list[str]) -> bool:
    """True when a parent of the target exists and is not a mapping."""
    parents = (_get(doc, ".".join(parts[:n])) for n in range(1, len(parts)))
    return any(p is not _MISSING and not isinstance(p, dict) for p in parents)


def _diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
    ))


def plan_set(scope: str, key: str, raw: str, text: str, validate: Callable[[str], list[str]]) -> Plan | PlanError:
    setting = setting_for(scope, key)
    if setting is None:
        return PlanError(f"unknown setting {scope}:{key}")
    doc = _load(text)
    if isinstance(doc, PlanError):
        return doc
    parts = _parts(doc, setting.path)
    if _blocked(doc, parts):
        return PlanError(f"{setting.path}: a parent key is not a mapping")
    value = _coerce(_get(doc, ".".join(parts)), raw)
    if isinstance(value, PlanError):
        return PlanError(f"{scope}:{key}: {value.reason}")
    new_text = yaml.safe_dump(_set_nested(doc, parts, value), sort_keys=False)
    errors = validate(new_text) if setting.scope == "cartridge" else []
    if errors:
        return PlanError(f"{setting.source_file} violates the schema: {'; '.join(errors)}")
    return Plan(new_text, _diff(text, new_text, setting.source_file), setting.pat_only)

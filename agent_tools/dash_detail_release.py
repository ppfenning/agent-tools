"""The data behind `cox dash --detail release`: the installed coxswain-tools
version, the umbrella manifest's component pins, the newest `v*` tag cut in
the umbrella checkout, and whether the Homebrew tap formula matches what is
installed. `release_facts` is the pure core: four raw strings (any may be
None), folded into the exact detail dict. `build` is the thin edge that reads
`importlib.metadata`, the routing profile, the umbrella manifest, `git
for-each-ref` and the tap formula.

`gate_progress` is always None: the store does not yet mark which work items
belong to a release, so coxtop's release frame shows a dash for it until the
release-manifest item decides where that lives.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from agent_tools import route

__all__ = ["build", "release_facts"]

DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"

_TAP_URL_RE = re.compile(r"coxswain_tools-([^\"'\s]+)\.tar\.gz")


def _pinned(manifest_text: str) -> dict:
    """`{component: tag}` from the umbrella manifest, `ref` for a component with no `tag`."""
    data = tomllib.loads(manifest_text)
    pinned = {"coxswain": data["coxswain"]["version"]}
    for name, table in data.get("components", {}).items():
        pinned[name] = table.get("tag") or table.get("ref")
    return pinned


def _last_cut(tag_line: str) -> dict:
    """`{"tag": ..., "at": ...}` from a `git for-each-ref` line, `at` converted to UTC ISO."""
    tag, _, at_raw = tag_line.strip().partition(" ")
    at = datetime.fromisoformat(at_raw).astimezone(UTC).isoformat()
    return {"tag": tag, "at": at}


def _tap(formula_text: str, version: str) -> dict | None:
    """`{"version": ..., "matches_installed": bool}` from the tap formula's first matching `url` line."""
    match = _TAP_URL_RE.search(formula_text)
    if match is None:
        return None
    found = match.group(1)
    return {"version": found, "matches_installed": found == version}


def release_facts(
    version: str,
    manifest_text: str | None,
    tag_line: str | None,
    formula_text: str | None,
) -> dict:
    """Fold the four raw reads (any may be None) into the exact `cox dash --detail release` dict."""
    return {
        "version": version,
        "pinned": None if manifest_text is None else _pinned(manifest_text),
        "last_cut": None if tag_line is None else _last_cut(tag_line),
        "tap": None if formula_text is None else _tap(formula_text, version),
        "gate_progress": None,
    }


def _profile_path() -> Path:
    return Path(os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser()


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _umbrella_dir() -> str | None:
    text = _read_text_or_none(_profile_path())
    profile = route.parse_profile(text) if text else {}
    return profile.get("umbrella_dir")


def _tag_line(umbrella: Path) -> str | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(umbrella),
                "for-each-ref",
                "--sort=-creatordate",
                "--count=1",
                "--format=%(refname:short) %(creatordate:iso-strict)",
                "refs/tags/v*",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    return line or None


def build(now: str) -> dict:
    """`now` is accepted for the caller's uniform call and unused."""
    del now
    version = metadata.version("coxswain-tools")
    umbrella_dir = _umbrella_dir()
    if not umbrella_dir:
        return release_facts(version, None, None, None)
    umbrella = Path(umbrella_dir).expanduser()
    manifest_text = _read_text_or_none(umbrella / "manifest.toml")
    tag_line = _tag_line(umbrella)
    formula_text = _read_text_or_none(umbrella.parent / "homebrew-coxswain" / "Formula" / "cox.rb")
    return release_facts(version, manifest_text, tag_line, formula_text)

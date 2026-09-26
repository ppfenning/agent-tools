"""The systemd user unit that keeps `cox chair run` alive. Pure text; the edge in cli.py does the file and PATH work."""

from __future__ import annotations

import re
from pathlib import Path

UNIT_NAME = "coxswain-chair.service"

# systemd splits ExecStart on whitespace and expands `%`, `$` and `\`; a value outside this set reaches the chair changed.
_UNIT_SAFE = re.compile(r"[A-Za-z0-9._@+:,/=-]+")


def unit_path(home: Path) -> Path:
    return home / ".config/systemd/user" / UNIT_NAME


def systemctl_lines() -> list[str]:
    return [
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {UNIT_NAME}",
        f"systemctl --user status {UNIT_NAME}",
    ]


def unsafe_values(values: dict[str, str]) -> list[str]:
    """The names whose value systemd would split or expand, in the order given."""
    return [name for name, value in values.items() if not _UNIT_SAFE.fullmatch(value)]


def export_lines(env_text: str) -> list[int]:
    """1-based numbers of `export NAME=value` lines; systemd's EnvironmentFile= does not document that prefix."""
    return [n for n, line in enumerate(env_text.splitlines(), start=1) if line.lstrip().startswith("export ")]


def unit_text(
    cox: str, workspace_dir: str, label: str, interval: int, environment_file: str | None, profile: str | None = None,
) -> str:
    """`EnvironmentFile=-` keeps a missing file from failing the unit."""
    env_lines = [f"EnvironmentFile=-{environment_file}"] if environment_file is not None else []
    profile_args = f" --profile {profile}" if profile is not None else ""
    lines = [
        "[Unit]",
        "Description=Coxswain autonomous chair",
        "",
        "[Service]",
        f"ExecStart={cox} chair run --label {label} --interval {interval}{profile_args}",
        f"WorkingDirectory={workspace_dir}",
        *env_lines,
        "Restart=on-failure",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"

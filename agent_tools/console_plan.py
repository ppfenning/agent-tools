"""Pure core for cox console: map a selection and key to the cox argv it runs.

Every write the console makes is a cox command it shells out to, so the
chair lease and the action log stay the one path. This module only plans
the argv; it does not run anything.
"""

from __future__ import annotations

import shlex


def plan_command(selection: dict, key: str) -> list[str] | None:
    kind = selection.get("kind")
    if kind == "draft" and key == "a":
        return ["cox", "route", "approve", selection["id"]]
    if kind == "draft" and key == "x":
        return [
            "cox",
            "route",
            "decline",
            selection["id"],
            "--reason",
            "declined from the console",
        ]
    if kind == "host" and key == "d":
        return ["cox", "host", "drain", selection["name"]]
    if kind == "host" and key == "u":
        return ["cox", "host", "activate", selection["name"]]
    if kind == "chair" and key == "t":
        return ["cox", "route", "chair", "take"]
    if kind == "chair" and key == "r":
        return ["cox", "route", "chair", "release"]
    if kind == "lane" and key == "s":
        # No cox command stops a lane; SIGTERM makes the run stamp its own
        # exit record, which is how the chair stops one today.
        return ["kill", "-TERM", str(selection["pid"])]
    return None


def confirm_line(argv: list[str]) -> str:
    return f"run: {shlex.join(argv)}? [y/N]"

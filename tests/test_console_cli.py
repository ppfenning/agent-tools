from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from agent_tools import cli, console_screen


def test_console_once_parses_with_the_three_documented_defaults() -> None:
    a = cli.build_parser().parse_args(["console", "--once"])
    assert (a.once, a.runs_dir, a.work_dir, a.interval) == (True, "runs", ".", 5.0)


def test_console_once_prints_the_rendered_sections_and_returns_zero(monkeypatch, capsys) -> None:
    sections = {"drafts": [], "hosts": [], "lanes": [], "chair": [], "needs_chair": []}
    monkeypatch.setattr(console_screen, "gather", lambda runs_dir, work_dir, now: sections)
    rc = cli.main(["console", "--once"])
    assert rc == 0
    assert capsys.readouterr().out == "\n".join(console_screen.render(sections, -1, 120, datetime.now(UTC), UTC)) + "\n"


def test_console_once_shows_a_host_beat_as_local_clock_plus_age(monkeypatch, capsys) -> None:
    seen = {}

    def gather(runs_dir, work_dir, now):
        seen["beat"] = (datetime.fromisoformat(now) - timedelta(seconds=120)).isoformat()
        host = {"name": "jarvis", "state": "up", "capacity": 2, "beat_at": seen["beat"], "versions_json": "{}"}
        return {"drafts": [], "hosts": [host], "lanes": [], "chair": [], "needs_chair": []}

    monkeypatch.setattr(console_screen, "gather", gather)
    assert cli.main(["console", "--once"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"beat=\d{1,2}:\d{2} [AP]M  2m ago", out)
    assert seen["beat"] not in out

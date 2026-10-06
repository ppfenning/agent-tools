from __future__ import annotations

import json
import re
import socket
from datetime import UTC, datetime, timedelta

from agent_tools import cli, console_screen, run_store, usage_window


def test_console_once_parses_with_the_three_documented_defaults() -> None:
    a = cli.build_parser().parse_args(["console", "--once"])
    assert (a.once, a.runs_dir, a.work_dir, a.interval) == (True, "runs", ".", 5.0)


def test_console_once_prints_the_rendered_sections_and_returns_zero(monkeypatch, capsys) -> None:
    sections = {"drafts": [], "hosts": [], "lanes": [], "chair": [], "needs_chair": []}
    monkeypatch.setattr(console_screen, "gather", lambda runs_dir, work_dir, now, local_name, local_capacity, spend: sections)
    rc = cli.main(["console", "--once"])
    assert rc == 0
    assert capsys.readouterr().out == "\n".join(console_screen.render(sections, -1, 120, datetime.now(UTC), UTC)) + "\n"


def test_console_once_passes_the_local_hostname_lane_cap_and_spend_to_gather(monkeypatch, capsys) -> None:
    seen = {}

    def gather(runs_dir, work_dir, now, local_name, local_capacity, spend):
        seen["local_name"] = local_name
        seen["local_capacity"] = local_capacity
        seen["spend"] = spend
        return {"drafts": [], "hosts": [], "lanes": [], "chair": [], "needs_chair": []}

    monkeypatch.setattr(console_screen, "gather", gather)
    monkeypatch.setattr(cli, "_console_spend", lambda runs_dir, profile, now: {"five_hour": 0.4, "weekly": None, "hard_stop": 0.93})
    assert cli.main(["console", "--once"]) == 0
    assert seen["local_name"] == socket.gethostname()
    assert seen["local_capacity"] == 3  # no profile, no policy.pacing.json: cli._CHAIR_MAX_IN_FLIGHT default
    assert seen["spend"] == {"five_hour": 0.4, "weekly": None, "hard_stop": 0.93}


def test_console_spend_matches_the_computation_chair_run_deps_uses(tmp_path) -> None:
    """`cli._console_spend` must never drift from what `_chair_run_deps`'s own `facts_deps.window`/`.weekly`/
    `.policy` lambdas compute (agent_tools/cli.py): the same three calls, on the same `runs_dir`/`profile`/`now`.
    No stub replaces `usage_window.gather`, `usage_window.gather_weekly` or `_resolved_pacing_policy` here; a
    ccusage-cache file (the real mechanism `usage_window._write_ccusage_cache` writes) stands in for a reachable
    `npx ccusage`, so the run-record fallback path is exercised with real `*.usage.json` data."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    (runs_dir / usage_window._CCUSAGE_CACHE_FILE).write_text(
        json.dumps({"at": now.isoformat(), "blocks": {"blocks": []}}), encoding="utf-8",
    )
    (runs_dir / "run-1.usage.json").write_text(
        json.dumps({"summary": {"cost_usd": 12.0, "started_at": (now - timedelta(hours=1)).isoformat()}}),
        encoding="utf-8",
    )
    (runs_dir / "policy.pacing.json").write_text(json.dumps({"weekly_hard_stop_fraction": 0.75}), encoding="utf-8")
    profile = {"window_ceiling_usd": 50.0, "weekly_ceiling_usd": 200.0, "weekly_reset": None}

    result = cli._console_spend(runs_dir, profile, now)

    # Independently built via the same functions `_chair_run_deps`'s `facts_deps` calls
    # (`window=`, `weekly=`, `policy=` lambdas), on the same runs_dir/profile/now.
    window = usage_window.gather(runs_dir, now, ceiling_usd=profile["window_ceiling_usd"])
    weekly = usage_window.gather_weekly(
        runs_dir, now, profile["weekly_ceiling_usd"],
        store_spend=lambda since: run_store.cost_since(runs_dir, since),
        reset=usage_window.parse_weekly_reset(profile["weekly_reset"]),
    )
    policy = cli._resolved_pacing_policy(runs_dir)

    assert result == {
        "five_hour": window.spent_usd / window.ceiling_usd,
        "weekly": weekly.spent_usd / weekly.ceiling_usd,
        "hard_stop": policy.weekly_hard_stop_fraction,
    }
    assert result == {"five_hour": 12.0 / 50.0, "weekly": 12.0 / 200.0, "hard_stop": 0.75}


def test_console_spend_reports_a_fraction_of_none_with_no_ceiling(tmp_path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    (runs_dir / usage_window._CCUSAGE_CACHE_FILE).write_text(
        json.dumps({"at": now.isoformat(), "blocks": {"blocks": []}}), encoding="utf-8",
    )
    result = cli._console_spend(runs_dir, {}, now)
    assert result == {"five_hour": None, "weekly": None, "hard_stop": usage_window.DEFAULT_POLICY.weekly_hard_stop_fraction}


def test_console_once_shows_a_host_beat_as_local_clock_plus_age(monkeypatch, capsys) -> None:
    seen = {}

    def gather(runs_dir, work_dir, now, local_name, local_capacity, spend):
        seen["beat"] = (datetime.fromisoformat(now) - timedelta(seconds=120)).isoformat()
        host = {"name": "jarvis", "state": "up", "capacity": 2, "beat_at": seen["beat"], "versions_json": "{}"}
        return {"drafts": [], "hosts": [host], "lanes": [], "chair": [], "needs_chair": []}

    monkeypatch.setattr(console_screen, "gather", gather)
    assert cli.main(["console", "--once"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"beat=\d{1,2}:\d{2} [AP]M  2m ago", out)
    assert seen["beat"] not in out


_EMPTY_SECTIONS = {"drafts": [], "hosts": [], "lanes": [], "chair": [], "needs_chair": []}


def _once_text(sections: dict) -> str:
    return "\n".join(console_screen.render(sections, -1, 120, datetime.now(UTC), UTC)) + "\n"


def _gather_returns_empty(runs_dir, work_dir, now, local_name, local_capacity, spend) -> dict:
    return _EMPTY_SECTIONS


def test_console_on_a_terminal_with_coxtop_execs_it_and_prints_nothing(monkeypatch, capsys) -> None:
    def gather_must_not_run(*args):
        raise AssertionError("gather ran although coxtop was launched")

    monkeypatch.setattr(console_screen, "gather", gather_must_not_run)
    calls = []
    rc = cli._console(cli.build_parser().parse_args(["console"]), launch=lambda: calls.append("launch") or True)
    assert (rc, calls, capsys.readouterr().out) == (0, ["launch"], "")


def test_console_without_a_terminal_prints_exactly_the_once_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(console_screen, "gather", _gather_returns_empty)
    assert cli._console(cli.build_parser().parse_args(["console"]), launch=lambda: False) == 0
    assert capsys.readouterr().out == _once_text(_EMPTY_SECTIONS)


def test_console_once_never_calls_the_launcher(monkeypatch, capsys) -> None:
    def launch_must_not_run() -> bool:
        raise AssertionError("--once reached the launcher")

    monkeypatch.setattr(console_screen, "gather", _gather_returns_empty)
    assert cli._console(cli.build_parser().parse_args(["console", "--once"]), launch=launch_must_not_run) == 0
    assert capsys.readouterr().out == _once_text(_EMPTY_SECTIONS)

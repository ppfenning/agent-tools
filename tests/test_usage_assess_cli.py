import json
from datetime import UTC, datetime

from agent_tools import cli
from agent_tools.pacing import Window

# Fixed in the past so `_usage_assess`'s own `datetime.now(UTC)` always falls
# after `_END`, pinning `elapsed_fraction` at 1.0 regardless of wall-clock
# time when the test runs.
_START = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
_END = datetime(2020, 1, 1, 10, 0, 0, tzinfo=UTC)


def _unmeasured_window() -> Window:
    return Window(start=_START, end=_END, spent_usd=1.0, ceiling_usd=None,
                  burn_usd_per_hour=0.5, runs_in_flight=1)


def _stopped_window() -> Window:
    return Window(start=_START, end=_END, spent_usd=95.0, ceiling_usd=10.0,
                  burn_usd_per_hour=90.0, runs_in_flight=3)


def test_go_against_an_unmeasured_window_exits_zero_and_names_the_verdict(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--runs-dir", "runs", "--profile", str(tmp_path / "no-profile.yaml")])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("go: ")


def test_stop_against_an_exhausted_window_exits_four(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _stopped_window())
    code = cli.main(["usage", "assess", "--profile", str(tmp_path / "no-profile.yaml")])
    out = capsys.readouterr().out
    assert code == 4
    assert out.startswith("stop: ")


def test_json_flag_prints_the_full_assessment(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["verdict"] == "go" and d["hold_until"] is None
    # No policy.pacing.json in tmp_path: falls back to DEFAULT_POLICY's own
    # ladders (deep/high), not a guessed tighter one.
    assert d["tier_ceiling"] == "deep" and d["effort_ceiling"] == "high"


def test_a_resolved_policy_file_in_the_runs_dir_changes_the_reported_ceilings(capsys, monkeypatch, tmp_path):
    (tmp_path / "policy.pacing.json").write_text(
        json.dumps({"tier_ladder": ["cheap"], "effort_ladder": ["low"]}), encoding="utf-8"
    )
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["tier_ceiling"] == "cheap" and d["effort_ceiling"] == "low"


def test_a_resolved_policy_files_hard_stop_fraction_is_honoured(capsys, monkeypatch, tmp_path):
    (tmp_path / "policy.pacing.json").write_text(
        json.dumps({"hard_stop_fraction": 0.9}), encoding="utf-8"
    )
    window = Window(start=_START, end=_END, spent_usd=95.0, ceiling_usd=100.0,
                     burn_usd_per_hour=0.0, runs_in_flight=1)
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: window)
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path),
                      "--profile", str(tmp_path / "no-profile.yaml")])
    d = json.loads(capsys.readouterr().out)
    assert code == 4
    assert d["verdict"] == "stop"
    assert "hard stop at 90%" in d["reason"]


def test_usage_assess_resolves_the_profiles_window_ceiling_usd_not_unmeasured(capsys, monkeypatch, tmp_path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("workspace_dir: /tmp/ws\nspend:\n  window_ceiling_usd: 50\n", encoding="utf-8")
    captured = {}

    def _gather(runs_dir, now, ceiling_usd=None, usage=None):
        captured["ceiling_usd"] = ceiling_usd
        return Window(start=_START, end=_END, spent_usd=5.0, ceiling_usd=ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=1)

    monkeypatch.setattr(cli.usage_window, "gather", _gather)
    code = cli.main(["usage", "assess", "--profile", str(profile_path), "--runs-dir", "runs"])
    out = capsys.readouterr().out
    assert code == 0
    assert captured["ceiling_usd"] == 50.0
    assert "unmeasured" not in out


def test_usage_assess_json_passes_the_profiles_parsed_weekly_reset_to_gather_weekly(capsys, monkeypatch, tmp_path):
    # `route.parse_profile`'s own grammar for `spend.weekly_reset` is out of
    # scope here; the profile dict it would produce is supplied directly so
    # this test exercises only `_usage_assess`'s wiring of that value through
    # to `gather_weekly`.
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("workspace_dir: /tmp/ws\n", encoding="utf-8")
    monkeypatch.setattr(cli.route, "parse_profile", lambda text: {"weekly_reset": "Sun 04:00 America/New_York"})
    captured = {}

    def _gather_weekly(runs_dir, now, weekly_ceiling_usd=None, usage=None, store_spend=None, reset=None):
        captured["reset"] = reset
        return Window(start=_START, end=_END, spent_usd=5.0, ceiling_usd=weekly_ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=1)

    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _unmeasured_window())
    monkeypatch.setattr(cli.usage_window, "gather_weekly", _gather_weekly)
    code = cli.main(["usage", "assess", "--json", "--runs-dir", str(tmp_path), "--profile", str(profile_path)])
    d = json.loads(capsys.readouterr().out)
    assert code == 0
    assert d["verdict"] == "go"
    assert captured["reset"] == cli.usage_window.parse_weekly_reset("Sun 04:00 America/New_York")

import datetime
import json

import pytest

from agent_tools import cli, route, steward

CLEARING_ROW = {
    "role": "build",
    "model": "sonnet-5",
    "ceiling": 2.00,
    "censored": False,
    "n_challenger": 20,
    "landed_rate_challenger": 0.91,
    "landed_rate_floor": 0.85,
    "window_days": 10,
    "strict": 1.50,
    "moderate": 1.80,
    "liberal": 2.40,
}

CLEARING_ROW_2 = {
    "role": "review",
    "model": "opus-4",
    "ceiling": 3.00,
    "censored": False,
    "n_challenger": 25,
    "landed_rate_challenger": 0.60,
    "landed_rate_floor": 0.90,
    "window_days": 12,
    "strict": 2.00,
    "moderate": 2.50,
    "liberal": 3.50,
}

SHORT_ROW = {
    "role": "review",
    "model": "haiku",
    "ceiling": 1.00,
    "censored": False,
    "n_challenger": 5,
    "landed_rate_challenger": 0.50,
    "landed_rate_floor": 0.50,
    "window_days": 5,
    "strict": 0.50,
    "moderate": 0.80,
    "liberal": 1.20,
}


def _write_profile(tmp_path, provider_profile):
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        f"provider_profile: {provider_profile}\n"
    )
    return profile, ws


def test_propose_writes_exactly_one_intake_file_for_the_clearing_candidate(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    profile, ws = _write_profile(tmp_path, provider_profile)
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [CLEARING_ROW, SHORT_ROW])

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    written = json.loads(out)
    assert len(written) == 1
    intake_files = list((ws / "intake").glob("*.md"))
    assert len(intake_files) == 1
    assert str(intake_files[0]) == written[0]
    fields, body = route.parse_frontmatter(intake_files[0].read_text())
    assert fields["title"] == "steward: raise build's ceiling for sonnet-5"
    assert fields["repo"] == "coxswain-tools"
    assert "id" in fields
    assert "Raise build's ceiling for sonnet-5 from 2.0 to 2.4." in body
    assert "| build | sonnet-5 | 20 | 0.91 | 0.85 | 10 |" in body


def test_propose_with_ids_sequence_writes_the_allocated_id(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    profile, ws = _write_profile(tmp_path, provider_profile)
    with profile.open("a") as f:
        f.write("ids: sequence\n")
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [CLEARING_ROW, SHORT_ROW])
    monkeypatch.setattr(route, "allocate_intake_id", lambda: "I7")

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    written = json.loads(out)
    assert written == [str(ws / "intake" / "I7.md")]


def test_propose_with_no_clearing_candidate_writes_nothing_and_exits_cleanly(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    profile, ws = _write_profile(tmp_path, provider_profile)
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [SHORT_ROW])

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert json.loads(out) == []
    assert not (ws / "intake").exists()


def test_propose_never_dumps_or_writes_the_provider_profile(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    before = provider_profile.read_text()
    profile, ws = _write_profile(tmp_path, provider_profile)
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [CLEARING_ROW])
    dumped = []
    monkeypatch.setattr(cli.yaml, "safe_dump", lambda *args, **kwargs: dumped.append(True))

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])

    assert rc == 0
    assert dumped == []
    assert provider_profile.read_text() == before


def _floor_call(run_id, i, role="build", model="sonnet-5"):
    return {
        "run_id": run_id, "role": role, "model": model, "task_id": f"{run_id}:p1:f{i}",
        "join_confidence": "heuristic", "challenger": 0,
    }


def _challenger_call(run_id, i, role="build", model="sonnet-5"):
    return {
        "run_id": run_id, "role": role, "model": model, "task_id": f"{run_id}:p1:c{i}",
        "join_confidence": "heuristic", "challenger": 1,
    }


def _landed_task(call, outcome="landed"):
    return {"run_id": call["run_id"], "task_id": call["task_id"], "outcome": outcome}


def test_steward_candidate_rows_joins_bounds_and_roles_reports_from_literal_calls():
    bound = {
        "role": "build", "model": "sonnet-5", "ceiling": 2.00, "censored": False,
        "strict": 1.50, "moderate": 1.80, "liberal": 2.40,
    }
    runs = [{"run_id": "r1", "started_at": "2026-09-01T00:00:00+00:00", "ended_at": "2026-09-05T00:00:00+00:00"}]
    calls = [_floor_call("r1", i) for i in range(15)] + [_challenger_call("r1", i) for i in range(5)]
    tasks = [_landed_task(c) for c in calls]

    rows = cli._steward_candidate_rows([bound], calls, tasks, runs)

    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "build"
    assert row["model"] == "sonnet-5"
    assert row["n_challenger"] == 5
    assert row["landed_rate_challenger"] == pytest.approx(1.0)
    assert row["landed_rate_floor"] == pytest.approx(1.0)
    assert row["window_days"] == 4
    for key in ("ceiling", "censored", "strict", "moderate", "liberal"):
        assert key in row


def test_steward_candidate_rows_drops_a_pair_with_no_challenger_row():
    bound = {
        "role": "build", "model": "sonnet-5", "ceiling": 2.00, "censored": False,
        "strict": 1.0, "moderate": 1.2, "liberal": 1.6,
    }
    runs = [{"run_id": "r1", "started_at": "2026-09-01T00:00:00+00:00", "ended_at": "2026-09-02T00:00:00+00:00"}]
    calls = [_floor_call("r1", i) for i in range(20)]
    tasks = [_landed_task(c) for c in calls]

    assert cli._steward_candidate_rows([bound], calls, tasks, runs) == []


def test_steward_candidate_rows_windows_to_the_most_recent_14_days_so_an_old_pair_still_clears_the_bar():
    """router-steward.md §5's window is measured from the pair's own latest
    run, not the pair's whole history: a pair with 40 days of history still
    clears the bar on its most recent 14 days of evidence."""
    bound = {
        "role": "build", "model": "sonnet-5", "ceiling": 2.00, "censored": False,
        "strict": 1.50, "moderate": 1.80, "liberal": 2.40,
    }
    runs = [
        {"run_id": "r-old", "started_at": "2026-08-01T00:00:00+00:00", "ended_at": "2026-08-02T00:00:00+00:00"},
        {"run_id": "r-recent", "started_at": "2026-09-10T00:00:00+00:00", "ended_at": "2026-09-11T00:00:00+00:00"},
    ]
    old_calls = [_floor_call("r-old", i) for i in range(20)] + [_challenger_call("r-old", i) for i in range(20)]
    recent_floor = [_floor_call("r-recent", i) for i in range(15)]
    recent_challenger = [_challenger_call("r-recent", i) for i in range(20)]
    calls = old_calls + recent_floor + recent_challenger
    tasks = (
        [_landed_task(c) for c in old_calls]
        + [_landed_task(c, "landed" if i < 9 else "quarantined") for i, c in enumerate(recent_floor)]
        + [_landed_task(c) for c in recent_challenger]
    )

    rows = cli._steward_candidate_rows([bound], calls, tasks, runs)

    assert len(rows) == 1
    row = rows[0]
    assert row["window_days"] == 1
    assert row["n_challenger"] == 20
    assert row["landed_rate_challenger"] == pytest.approx(1.0)
    assert row["landed_rate_floor"] == pytest.approx(0.6)
    policy = {"min_n": 20, "window_days_cap": 14, "landed_rate_delta_floor": 0.05}
    assert len(steward.ceiling_candidates(rows, policy)) == 1


def test_propose_writes_a_distinct_file_per_candidate_in_the_same_run(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    profile, ws = _write_profile(tmp_path, provider_profile)
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [CLEARING_ROW, CLEARING_ROW_2])

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    written = json.loads(out)
    assert len(written) == 2
    assert len(set(written)) == 2
    intake_files = sorted((ws / "intake").glob("*.md"))
    assert len(intake_files) == 2
    titles = {route.parse_frontmatter(p.read_text())[0]["title"] for p in intake_files}
    assert titles == {
        "steward: raise build's ceiling for sonnet-5",
        "steward: lower review's ceiling for opus-4",
    }


def test_propose_refuses_to_overwrite_an_existing_proposal_for_the_same_candidate(tmp_path, monkeypatch, capsys):
    provider_profile = tmp_path / "provider.yaml"
    provider_profile.write_text("budget_usd:\n  standard: 2.0\n")
    profile, ws = _write_profile(tmp_path, provider_profile)
    monkeypatch.setattr(cli, "_steward_bounds_rows_for", lambda a: [CLEARING_ROW])
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    title = "steward: raise build's ceiling for sonnet-5"
    mapping = route.intake_file(title, "stale proposal from an earlier run", "coxswain-tools", date)
    (rel,) = mapping
    existing_path = ws / rel
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text(mapping[rel])
    before = existing_path.read_text()

    rc = cli.main(["steward", "propose", "--profile", str(profile), "--json"])
    out = capsys.readouterr().out

    assert rc == 2
    assert "refusing to overwrite" in out
    assert existing_path.read_text() == before
    assert list((ws / "intake").glob("*.md")) == [existing_path]

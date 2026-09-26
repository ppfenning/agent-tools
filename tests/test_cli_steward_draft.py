import json

from agent_tools import cli, draft_render, route, steward_draft

NOW = "2026-09-26T10:00:00Z"

PROPOSAL = {
    "id": "raise-build-ceiling",
    "title": "Raise the build ceiling",
    "repo": "coxswain-tools",
    "files": ["agent_tools/steward.py"],
    "evidence": ["stats.db calls table, run r1"],
    "tickets": [
        {
            "phase": "p1",
            "id": "first",
            "title": "First step",
            "needs": [],
            "current": "Now.",
            "change": "Change it.",
            "tests": "Test it.",
            "done_when": "Done.",
        },
        {
            "phase": "p2",
            "id": "second",
            "title": "Second step",
            "needs": ["first"],
            "current": "Then.",
            "change": "Change more.",
            "tests": "Test more.",
            "done_when": "Done too.",
        },
    ],
}


def _setup(tmp_path, monkeypatch, proposals):
    ws = tmp_path / "workspace"
    (ws / "intake").mkdir(parents=True)
    for name, proposal in proposals.items():
        (ws / "intake" / f"{name}.json").write_text(json.dumps(proposal))
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        f"provider_profile: {tmp_path / 'provider.yaml'}\n"
    )
    monkeypatch.setattr(steward_draft, "now_utc", lambda: NOW)
    return profile, ws


def _run(profile, capsys, *extra):
    rc = cli.main(["steward", "draft", "--profile", str(profile), *extra])
    return rc, capsys.readouterr().out


def _draft_files(ws):
    return sorted(p for p in (ws / "work").rglob("*.md")) if (ws / "work").exists() else []


def test_grounded_proposal_is_written_as_a_draft_with_todo_tickets(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {"a": PROPOSAL})
    slug = draft_render.draft_id(PROPOSAL)

    rc, out = _run(profile, capsys)

    assert rc == 0
    assert f"drafted (1):\n  {slug}" in out
    fields, _ = route.parse_frontmatter((ws / "work" / slug / "initiative.md").read_text())
    assert fields["draft"] == "true"
    assert fields["proposed_by"] == "steward"
    assert fields["proposed_at"] == NOW
    for phase, task in (("p1", "first"), ("p2", "second")):
        ticket, _ = route.parse_frontmatter((ws / "work" / slug / phase / f"{task}.md").read_text())
        assert ticket["state"] == "todo"


def test_proposal_with_no_repo_is_listed_and_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {"a": {k: v for k, v in PROPOSAL.items() if k != "repo"}})

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out)["ungrounded"] == [{"id": "raise-build-ceiling", "reason": "no repo"}]
    assert _draft_files(ws) == []


def test_proposal_with_no_file_is_listed_and_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {"a": {**PROPOSAL, "files": []}})

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out)["ungrounded"] == [{"id": "raise-build-ceiling", "reason": "no file"}]
    assert _draft_files(ws) == []


def test_second_run_leaves_the_existing_draft_bytes_and_reports_exists(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {"a": PROPOSAL})
    _run(profile, capsys)
    before = {p: p.read_bytes() for p in _draft_files(ws)}
    monkeypatch.setattr(steward_draft, "now_utc", lambda: "2026-09-27T10:00:00Z")

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out) == {"drafted": [], "exists": [draft_render.draft_id(PROPOSAL)], "ungrounded": []}
    assert {p: p.read_bytes() for p in _draft_files(ws)} == before


def test_json_gives_the_three_lists(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(
        tmp_path,
        monkeypatch,
        {"a": PROPOSAL, "b": {**PROPOSAL, "id": "other", "files": []}, "c": {**PROPOSAL, "id": "old"}},
    )
    (ws / "work" / draft_render.draft_id({"id": "old"})).mkdir(parents=True)

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out) == {
        "drafted": [draft_render.draft_id(PROPOSAL)],
        "exists": [draft_render.draft_id({"id": "old"})],
        "ungrounded": [{"id": "other", "reason": "no file"}],
    }


def test_steward_propose_intake_file_is_listed_as_ungrounded(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {})
    (ws / "intake" / "2026-09-26-steward-raise.md").write_text(
        "---\nid: steward-raise\ntitle: \"steward: raise\"\nrepo: coxswain-tools\n---\nRaise it.\n"
    )

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out)["ungrounded"] == [{"id": "steward-raise", "reason": "no file"}]
    assert _draft_files(ws) == []


def test_unreadable_json_is_listed_and_a_missing_profile_refuses(tmp_path, monkeypatch, capsys):
    profile, ws = _setup(tmp_path, monkeypatch, {})
    (ws / "intake" / "broken.json").write_text("{not json")

    rc, out = _run(profile, capsys, "--json")

    assert rc == 0
    assert json.loads(out)["ungrounded"][0]["id"] == "broken"
    assert cli.main(["steward", "draft", "--profile", str(tmp_path / "missing.yaml")]) == 2

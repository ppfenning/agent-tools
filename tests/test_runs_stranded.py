import argparse
import json
import subprocess

from agent_tools import cli, runs_stranded


def _record(**over):
    base = {
        "run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "landed": False,
    }
    return {**base, **over}


def _item(state, **over):
    base = {"id": "t1", "state": state, "initiative": "acme", "phase": "p1", "repo": "/repo/acme"}
    return {**base, **over}


def test_an_approved_unlanded_record_with_a_ready_item_is_listed_with_its_remedy():
    rows = runs_stranded.stranded([_record()], [_item("ready")])
    assert rows == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
                      "remedy": "cox runs land r1 --task t1 --repo /repo/acme"}]


def test_an_approved_record_a_code_or_ticket_quarantine_superseded_is_not_stranded():
    for cause in ("code", "ticket"):
        assert runs_stranded.stranded([_record(cause=cause)], [_item("ready")]) == []


def test_an_approved_record_with_a_harness_cause_is_still_stranded():
    assert [r["task"] for r in runs_stranded.stranded([_record(cause="harness")], [_item("ready")])] == ["t1"]


def test_a_null_arbitration_record_with_both_reviewers_approving_is_stranded():
    record = _record(arbitration=None, adversary={"verdict": "approve"})
    assert [r["task"] for r in runs_stranded.stranded([record], [_item("ready")])] == ["t1"]


def test_a_null_arbitration_record_with_a_reviewer_asking_for_a_revision_is_not_stranded():
    record = _record(arbitration=None, adversary={"verdict": "revise"})
    assert runs_stranded.stranded([record], [_item("ready")]) == []


def test_a_null_review_record_with_no_arbitration_does_not_crash_and_is_not_stranded():
    assert runs_stranded.stranded([_record(review=None, arbitration=None)], [_item("ready")]) == []


def test_an_arbiter_skipped_record_counts_as_approved_and_is_stranded():
    record = _record(arbitration="arbiter: skipped (both approved)")
    rows = runs_stranded.stranded([record], [_item("ready")])
    assert [r["task"] for r in rows] == ["t1"]


def test_a_done_item_is_not_stranded():
    assert runs_stranded.stranded([_record()], [_item("done")]) == []


def test_a_revise_verdict_is_not_stranded():
    record = _record(review={"verdict": "revise"}, arbitration={"verdict": "revise"})
    assert runs_stranded.stranded([record], [_item("ready")]) == []


def test_an_arbiter_approval_over_a_revise_review_is_stranded_as_the_land_would_land_it():
    record = _record(review={"verdict": "revise"}, arbitration={"verdict": "approve"})
    assert [r["task"] for r in runs_stranded.stranded([record], [_item("ready")])] == ["t1"]


def test_a_landed_record_is_not_stranded():
    record = _record(landed=True)
    assert runs_stranded.stranded([record], [_item("ready")]) == []


def test_the_records_own_repo_wins_over_the_items_repo():
    rows = runs_stranded.stranded([_record(repo="/repo/x")], [_item("ready")])
    assert rows[0]["remedy"] == "cox runs land r1 --task t1 --repo /repo/x"


def test_an_item_with_no_resolvable_repo_is_reported_with_remedy_none():
    rows = runs_stranded.stranded([_record()], [_item("ready", repo=None)])
    assert rows[0]["remedy"] is None


def test_an_id_shared_across_initiatives_scopes_to_the_records_own_initiative():
    record = _record(initiative="other")
    items = [_item("done", initiative="acme", repo="/repo/acme"),
             _item("ready", initiative="other", repo="/repo/other")]
    rows = runs_stranded.stranded([record], items)
    assert rows == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
                      "remedy": "cox runs land r1 --task t1 --repo /repo/other"}]


def test_an_id_shared_across_initiatives_with_no_initiative_on_the_record_is_reported_not_dropped():
    items = [_item("done", initiative="acme"), _item("ready", initiative="other")]
    rows = runs_stranded.stranded([_record()], items)
    assert len(rows) == 1
    assert rows[0]["remedy"] is None


def test_an_id_shared_across_phases_of_the_same_initiative_scopes_to_the_records_own_phase():
    record = _record(phase="p2")
    items = [_item("done", phase="p1", repo="/repo/wrong"),
             _item("ready", phase="p2", repo="/repo/right")]
    rows = runs_stranded.stranded([record], items)
    assert rows[0]["remedy"] == "cox runs land r1 --task t1 --repo /repo/right"


def test_a_record_whose_task_moved_to_a_blocked_item_in_another_phase_is_not_stranded():
    record = _record(initiative="acme", phase="adapters")
    assert runs_stranded.stranded([record], [_item("blocked", phase="adapters-openai")]) == []


def test_a_record_whose_task_moved_to_a_ready_item_in_another_phase_takes_that_items_remedy():
    record = _record(initiative="acme", phase="adapters")
    rows = runs_stranded.stranded([record], [_item("ready", phase="adapters-openai")])
    assert rows[0]["remedy"] == "cox runs land r1 --task t1 --repo /repo/acme"


def test_a_dropped_item_with_an_approved_record_is_not_stranded():
    assert runs_stranded.stranded([_record()], [_item("dropped")]) == []


def test_an_approved_item_is_still_stranded():
    rows = runs_stranded.stranded([_record()], [_item("approved")])
    assert rows == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
                      "remedy": "cox runs land r1 --task t1 --repo /repo/acme"}]


def test_a_record_with_a_missing_repo_is_skipped():
    record = _record(repo="/gone")
    assert runs_stranded.stranded([record], [_item("ready")], repo_exists=lambda p: False) == []


def test_a_present_repo_or_no_predicate_leaves_the_record_listed():
    record = _record(repo="/here")
    assert len(runs_stranded.stranded([record], [_item("ready")], repo_exists=lambda p: True)) == 1
    assert len(runs_stranded.stranded([record], [_item("ready")], repo_exists=None)) == 1


def test_a_record_with_no_repo_is_never_skipped_for_a_missing_one():
    rows = runs_stranded.stranded([_record()], [_item("ready")], repo_exists=lambda p: False)
    assert len(rows) == 1


def test_missing_repos_returns_a_shared_path_once():
    records = [_record(run="r1", repo="/gone"), _record(run="r2", repo="/gone")]
    assert runs_stranded.missing_repos(records, [_item("ready")], lambda p: False) == ["/gone"]


def test_missing_repos_omits_a_dropped_items_repo():
    record = _record(repo="/gone")
    assert runs_stranded.missing_repos([record], [_item("dropped")], lambda p: False) == []


def test_a_phase_branch_the_predicate_reports_as_existing_is_skipped():
    record = _record(branch="epic/p1")
    rows = runs_stranded.stranded([record], [_item("ready")], branch_exists=lambda repo, branch: True)
    assert rows == []


def test_the_same_phase_branch_reported_gone_is_still_listed():
    record = _record(branch="epic/p1")
    rows = runs_stranded.stranded([record], [_item("ready")], branch_exists=lambda repo, branch: False)
    assert len(rows) == 1


def test_a_task_scoped_branch_is_listed_regardless_of_the_predicate():
    record = _record(branch="agents/r1/t1")
    rows = runs_stranded.stranded([record], [_item("ready")], branch_exists=lambda repo, branch: True)
    assert len(rows) == 1


def test_an_epic_task_merge_target_branch_is_listed_even_when_the_predicate_reports_it_present():
    record = _record(branch="epic/acme/p1--t1")
    rows = runs_stranded.stranded([record], [_item("ready")], branch_exists=lambda repo, branch: True)
    assert len(rows) == 1


def test_branch_exists_is_asked_about_the_items_repo_when_the_record_has_none():
    calls = []
    record = _record(branch="epic/acme/p1")
    rows = runs_stranded.stranded([record], [_item("ready")],
                                  branch_exists=lambda repo, branch: calls.append((repo, branch)) is None)
    assert rows == []
    assert calls == [("/repo/acme", "epic/acme/p1")]


def test_omitting_branch_exists_reproduces_todays_list_unchanged():
    record = _record(branch="epic/p1")
    with_none = runs_stranded.stranded([record], [_item("ready")], branch_exists=None)
    without_arg = runs_stranded.stranded([record], [_item("ready")])
    assert with_none == without_arg
    assert len(without_arg) == 1


def _workspace(tmp_path, with_stranded, item_state="ready", record_repo=None, branch=None):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path / 'ws'}\n", encoding="utf-8")
    ws = tmp_path / "ws"
    if with_stranded:
        tasks = ws / "runs" / "r1" / "tasks" / "p1"
        tasks.mkdir(parents=True)
        over = {}
        if record_repo is not None:
            over["repo"] = record_repo
        if branch is not None:
            over["branch"] = branch
        record = _record(**over)
        (tasks / "t1.json").write_text(json.dumps(record), encoding="utf-8")
        work = ws / "work" / "acme" / "p1"
        work.mkdir(parents=True)
        (work / "t1.md").write_text(f"---\nid: t1\nstate: {item_state}\n---\nbody\n", encoding="utf-8")
        (ws / "work" / "acme" / "initiative.md").write_text("---\nrepo: /repo/acme\n---\nbody\n", encoding="utf-8")
    else:
        (ws / "runs").mkdir(parents=True)
        (ws / "work").mkdir(parents=True)
    return profile


def test_cli_json_lists_the_stranded_row(tmp_path, capsys):
    profile = _workspace(tmp_path, with_stranded=True)
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=True))
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
                      "remedy": "cox runs land r1 --task t1 --repo /repo/acme"}]


def test_cli_prints_no_stranded_work_and_exits_zero_on_an_empty_workspace(tmp_path, capsys):
    profile = _workspace(tmp_path, with_stranded=False)
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=False))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "no stranded work"


def test_cli_exits_two_when_the_profile_is_unreadable(tmp_path):
    rc = cli._runs_stranded(argparse.Namespace(profile=str(tmp_path / "absent.yaml"), runs_dir=None, json=False))
    assert rc == 2


def test_cli_prints_no_stranded_work_when_the_items_state_is_dropped(tmp_path, capsys):
    profile = _workspace(tmp_path, with_stranded=True, item_state="dropped")
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=False))
    assert rc == 0
    out, err = capsys.readouterr()
    assert out.strip() == "no stranded work"
    assert err == ""


def test_cli_skips_a_missing_repo_and_names_it_on_stderr(tmp_path, capsys):
    missing_repo = str(tmp_path / "gone")
    profile = _workspace(tmp_path, with_stranded=True, record_repo=missing_repo)
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=False))
    assert rc == 0
    out, err = capsys.readouterr()
    assert out.strip() == "no stranded work"
    assert err.strip() == f"skipped missing repo: {missing_repo}"


def _git_repo_with_branch(repo, branch):
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", branch], check=True)


def test_cli_prints_no_stranded_work_when_the_phase_branch_still_exists(tmp_path, capsys):
    repo = tmp_path / "repo"
    _git_repo_with_branch(repo, "epic/acme/p1")
    profile = _workspace(tmp_path, with_stranded=True, record_repo=str(repo), branch="epic/acme/p1")
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=False))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "no stranded work"


def test_cli_lists_the_row_once_the_phase_branch_is_deleted(tmp_path, capsys):
    repo = tmp_path / "repo"
    _git_repo_with_branch(repo, "epic/acme/p1")
    subprocess.run(["git", "-C", str(repo), "branch", "-D", "epic/acme/p1"], check=True)
    profile = _workspace(tmp_path, with_stranded=True, record_repo=str(repo), branch="epic/acme/p1")
    rc = cli._runs_stranded(argparse.Namespace(profile=str(profile), runs_dir=None, json=True))
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "epic/acme/p1",
                      "remedy": f"cox runs land r1 --task t1 --repo {repo}"}]

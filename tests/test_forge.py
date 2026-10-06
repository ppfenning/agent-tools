import json
import subprocess
from types import SimpleNamespace

from agent_tools import forge, forge_github, forge_local, route


def test_forge_name_defaults_to_local():
    assert forge.forge_name({}) == "local"


def test_forge_name_reads_the_profiles_forge_key():
    assert forge.forge_name({"forge": "github"}) == "github"


def test_forge_for_returns_the_builtin_module():
    assert forge.forge_for("github") is forge_github


def test_forge_for_is_none_for_an_unknown_name():
    assert forge.forge_for("no-such-forge") is None


def test_forge_for_finds_an_entry_point_forge(monkeypatch):
    module = SimpleNamespace(name="acme forge")
    registered = [SimpleNamespace(name="acme", load=lambda: module)]
    monkeypatch.setattr(forge.importlib.metadata, "entry_points", lambda group: registered if group == "coxswain.forges" else [])
    assert forge.forge_for("acme") is module
    assert forge.forge_for("other") is None


def test_parse_profile_accepts_a_forge_line():
    assert route.parse_profile("forge: github\n")["forge"] == "github"


STEP = {"branch": "pr/t", "default_branch": "main"}


def _recording(monkeypatch, answers=()):
    calls, queue = [], list(answers)

    def run(argv, **kwargs):
        calls.append(argv)
        code, out = queue.pop(0) if queue else (0, "")
        return SimpleNamespace(returncode=code, stdout=out, stderr=out)

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def test_github_open_pr_passes_head_and_base_when_given(monkeypatch, tmp_path):
    calls = _recording(monkeypatch)
    forge_github.open_pr(tmp_path, "t", "b", head="pr/t", base="main")
    assert calls[0][-4:] == ["--head", "pr/t", "--base", "main"]


def test_github_open_pr_names_neither_ref_by_default(monkeypatch, tmp_path):
    calls = _recording(monkeypatch)
    forge_github.open_pr(tmp_path, "t", "b")
    assert calls[0] == ["gh", "pr", "create", "--title", "t", "--body", "b"]


def _merge_argvs(repo, update):
    return [["gh", "pr", "merge", "pr/t", "--squash", "--delete-branch"],
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
            ["git", "-C", str(repo), *update]]


def test_github_merge_names_the_branch_and_pulls_when_on_the_default_branch(monkeypatch, tmp_path):
    calls = _recording(monkeypatch, [(0, "merged"), (0, "main"), (0, "Already up to date.")])
    assert forge_github.merge(tmp_path, STEP) == (True, "merged\nAlready up to date.")
    assert calls == _merge_argvs(tmp_path, ["pull", "--ff-only", "origin", "main"])


def test_github_merge_fetches_the_default_branch_when_on_another_branch(monkeypatch, tmp_path):
    calls = _recording(monkeypatch, [(0, "merged"), (0, "pr/t"), (0, "updated")])
    assert forge_github.merge(tmp_path, STEP) == (True, "merged\nupdated")
    assert calls == _merge_argvs(tmp_path, ["fetch", "origin", "main:main"])


def test_github_merge_fetches_when_head_is_detached(monkeypatch, tmp_path):
    calls = _recording(monkeypatch, [(0, "merged"), (128, "fatal: ref HEAD is not a symbolic ref"), (0, "")])
    assert forge_github.merge(tmp_path, STEP) == (True, "merged")
    assert calls == _merge_argvs(tmp_path, ["fetch", "origin", "main:main"])


def test_github_merge_stays_ok_when_the_local_update_fails(monkeypatch, tmp_path):
    _recording(monkeypatch, [(0, "merged"), (0, "main"), (1, "not fast-forward")])
    ok, detail = forge_github.merge(tmp_path, STEP)
    assert ok is True and detail == "merged\nlocal main not updated: not fast-forward"


def test_github_merge_fails_and_updates_nothing_when_gh_fails(monkeypatch, tmp_path):
    calls = _recording(monkeypatch, [(1, "no such pr")])
    assert forge_github.merge(tmp_path, STEP) == (False, "no such pr")
    assert calls == _merge_argvs(tmp_path, [])[:1]


def test_github_read_checks_rev_parses_the_given_ref(monkeypatch, tmp_path):
    calls = _recording(monkeypatch, [(1, "bad ref")])
    assert forge_github._read_checks(tmp_path, "pr/t") == (False, "bad ref")
    assert calls == [["git", "-C", str(tmp_path), "rev-parse", "pr/t"]]


# --- required checks and the one rerun ---

RULESET = '[{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "test"}, {"context": "lint"}]}}, {"type": "deletion"}]'
PROTECTION = '{"contexts": ["test"], "checks": [{"context": "test", "app_id": 1}, {"context": "build", "app_id": 2}]}'
RUN_URL = "https://github.com/o/r/actions/runs/{}/job/9"


def _runs(*rows, first_id=1):
    """A check-runs body; a rerun gives its check runs new ids, so a later attempt passes a new `first_id`."""
    return json.dumps({"total_count": len(rows), "check_runs": [
        {"id": first_id + i, "name": n, "status": "completed", "conclusion": c, "details_url": u}
        for i, (n, c, u) in enumerate(rows)]})


NO_STATUS = (0, '{"total_count": 0, "state": "pending", "statuses": []}')
BASE_MAIN = (0, '{"baseRefName": "main"}')
EMPTY_PROTECTION = (0, '{"contexts": [], "checks": []}')


def _scripted(monkeypatch, replies):
    """Record argv; `replies` maps a substring of the joined argv to `(code, stdout)` answers, the last repeating."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        line = " ".join(argv)
        for key, queue in replies.items():
            if key in line:
                code, out = queue.pop(0) if len(queue) > 1 else queue[0]
                return SimpleNamespace(returncode=code, stdout=out, stderr=out)
        return SimpleNamespace(returncode=1, stdout="", stderr="unscripted")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def _wait(monkeypatch, tmp_path, check_runs, status=NO_STATUS, required=((0, "[]"), EMPTY_PROTECTION), rerun=((0, ""),)):
    """`rerun` is a list of answers for every `gh run rerun`, or a dict of answers keyed by a substring of the argv."""
    calls = _scripted(monkeypatch, {
        "pr view": [BASE_MAIN], "rules/branches": [required[0]], "protection/required": [required[1]],
        "rev-parse": [(0, "abc\n")], "check-runs": [(0, body) for body in check_runs], "/status": [status],
        **(rerun if isinstance(rerun, dict) else {"run rerun": list(rerun)})})
    result = forge_github.wait_checks(tmp_path, 60, sleep=lambda s: None, now=lambda: 0.0)
    return result, [c for c in calls if c[:3] == ["gh", "run", "rerun"]], calls


def test_parse_ruleset_contexts_reads_each_required_status_checks_rule():
    assert forge_github.parse_ruleset_contexts(RULESET) == ("test", "lint")


def test_parse_ruleset_contexts_is_empty_for_no_rules_and_none_for_garbage():
    assert forge_github.parse_ruleset_contexts("[]") == ()
    assert forge_github.parse_ruleset_contexts("not json") is None
    assert forge_github.parse_ruleset_contexts('{"message": "Not Found"}') is None


def test_parse_protection_contexts_unions_contexts_and_checks():
    assert forge_github.parse_protection_contexts(PROTECTION) == ("test", "build")
    assert forge_github.parse_protection_contexts("") is None


def test_read_required_uses_the_ruleset_when_it_names_contexts(monkeypatch, tmp_path):
    calls = _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(0, RULESET)]})
    assert forge_github._read_required(tmp_path, "pr/t") == ("test", "lint")
    assert calls[1] == ["gh", "api", "repos/{owner}/{repo}/rules/branches/main"]
    assert len(calls) == 2


def test_read_required_falls_back_to_protection_when_the_ruleset_is_empty(monkeypatch, tmp_path):
    calls = _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(0, "[]")], "protection/required": [(0, PROTECTION)]})
    assert forge_github._read_required(tmp_path, "pr/t") == ("test", "build")
    assert calls[2] == ["gh", "api", "repos/{owner}/{repo}/branches/main/protection/required_status_checks"]


def test_read_required_is_empty_only_when_both_endpoints_read_nothing(monkeypatch, tmp_path):
    _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(0, "[]")], "protection/required": [EMPTY_PROTECTION]})
    assert forge_github._read_required(tmp_path, "pr/t") == ()


def test_read_required_is_none_when_both_endpoints_are_unreadable(monkeypatch, tmp_path):
    _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(1, "boom")], "protection/required": [(0, "<html>")]})
    assert forge_github._read_required(tmp_path, "pr/t") is None


def test_read_required_is_none_when_the_ruleset_is_unreadable_even_if_protection_names_contexts(monkeypatch, tmp_path):
    calls = _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(1, "502")], "protection/required": [(0, PROTECTION)]})
    assert forge_github._read_required(tmp_path, "pr/t") is None
    assert len(calls) == 2


def test_read_required_is_none_when_the_ruleset_is_empty_and_protection_is_unreadable(monkeypatch, tmp_path):
    _scripted(monkeypatch, {"pr view": [BASE_MAIN], "rules/branches": [(0, "[]")], "protection/required": [(1, "502")]})
    assert forge_github._read_required(tmp_path, "pr/t") is None


def test_read_required_is_none_when_the_base_cannot_be_read(monkeypatch, tmp_path):
    _scripted(monkeypatch, {"pr view": [(1, "no pull requests found")]})
    assert forge_github._read_required(tmp_path, "pr/t") is None


def test_rerun_run_ids_dedupes_and_skips_failures_without_a_run_url():
    body = json.loads(_runs(("a", "failure", RUN_URL.format(7)), ("b", "failure", RUN_URL.format(7)),
                            ("c", "cancelled", RUN_URL.format(8)), ("d", "success", RUN_URL.format(9)),
                            ("e", "failure", "https://example.com/build/1")))
    assert forge_github.rerun_run_ids(body) == ("7", "8")


def test_wait_checks_follows_the_old_rule_when_required_is_unreadable(monkeypatch, tmp_path):
    green = _runs(("test", "success", RUN_URL.format(1)))
    result, reruns, _ = _wait(monkeypatch, tmp_path, [green], required=((1, "boom"), (1, "boom")))
    assert result == (True, "green")
    assert reruns == []


def test_wait_checks_reruns_a_failed_run_once_and_keeps_polling(monkeypatch, tmp_path):
    failed, green = _runs(("test", "failure", RUN_URL.format(123))), _runs(("test", "success", RUN_URL.format(123)))
    result, reruns, _ = _wait(monkeypatch, tmp_path, [failed, green])
    assert result == (True, "green")
    assert reruns == [["gh", "run", "rerun", "123", "--failed"]]


def test_wait_checks_returns_the_failure_after_the_rerun_without_a_second_rerun(monkeypatch, tmp_path):
    failed, failed_again = _runs(("test", "failure", RUN_URL.format(123))), _runs(("test", "failure", RUN_URL.format(123)), first_id=10)
    result, reruns, _ = _wait(monkeypatch, tmp_path, [failed, failed_again])
    assert result == (False, "failing checks: test")
    assert reruns == [["gh", "run", "rerun", "123", "--failed"]]


def test_wait_checks_keeps_polling_while_the_failure_is_the_one_already_rerun(monkeypatch, tmp_path):
    failed, failed_again = _runs(("test", "failure", RUN_URL.format(123))), _runs(("test", "failure", RUN_URL.format(123)), first_id=10)
    result, reruns, calls = _wait(monkeypatch, tmp_path, [failed, failed, failed_again])
    assert result == (False, "failing checks: test")
    assert len(reruns) == 1
    assert sum(any("check-runs" in a for a in c) for c in calls) == 3


def test_wait_checks_ends_a_stale_failure_after_the_poll_error_limit(monkeypatch, tmp_path):
    failed = _runs(("test", "failure", RUN_URL.format(123)))
    result, reruns, calls = _wait(monkeypatch, tmp_path, [failed])
    assert result == (False, "failing checks: test")
    assert len(reruns) == 1
    assert sum(any("check-runs" in a for a in c) for c in calls) == 2 + forge_github.land.POLL_ERROR_LIMIT


def test_wait_checks_retries_a_refused_rerun_and_counts_the_accepted_one(monkeypatch, tmp_path):
    failed, green = _runs(("test", "failure", RUN_URL.format(123))), _runs(("test", "success", RUN_URL.format(123)), first_id=10)
    result, reruns, _ = _wait(monkeypatch, tmp_path, [failed, failed, green], rerun=[(1, "run 123 cannot be rerun; it is in progress"), (0, "")])
    assert result == (True, "green")
    assert reruns == [["gh", "run", "rerun", "123", "--failed"]] * 2


def test_wait_checks_fails_when_every_rerun_is_refused_up_to_the_limit(monkeypatch, tmp_path):
    failed = _runs(("test", "failure", RUN_URL.format(123)))
    result, reruns, _ = _wait(monkeypatch, tmp_path, [failed], rerun=[(1, "cannot be rerun")])
    assert result == (False, "failing checks: test")
    assert len(reruns) == forge_github.land.POLL_ERROR_LIMIT


def test_wait_checks_never_reruns_an_accepted_run_again_after_a_partial_refusal(monkeypatch, tmp_path):
    failed = _runs(("unit", "failure", RUN_URL.format(5)), ("lint", "failure", RUN_URL.format(6)))
    green = _runs(("unit", "success", RUN_URL.format(5)), ("lint", "success", RUN_URL.format(6)), first_id=10)
    result, reruns, _ = _wait(monkeypatch, tmp_path, [failed, failed, green],
                              rerun={"run rerun 5 ": [(0, "")], "run rerun 6 ": [(1, "in progress"), (0, "")]})
    assert result == (True, "green")
    assert reruns == [["gh", "run", "rerun", "5", "--failed"], ["gh", "run", "rerun", "6", "--failed"],
                      ["gh", "run", "rerun", "6", "--failed"]]


def test_wait_checks_passes_none_to_the_poll_when_required_is_unreadable(monkeypatch, tmp_path):
    status = (0, '{"total_count": 1, "state": "failure", "statuses": [{"context": "ci", "state": "failure"}]}')
    note = "required checks could not be read"
    unreadable, _, _ = _wait(monkeypatch, tmp_path, [_runs()], status=status, required=((1, "boom"), (1, "boom")))
    nothing, _, _ = _wait(monkeypatch, tmp_path, [_runs()], status=status)
    assert unreadable[0] is False and note in unreadable[1]
    assert note not in nothing[1]


def test_base_ref_argv_leaves_head_to_gh():
    assert forge_github.base_ref_argv("HEAD") == ["gh", "pr", "view", "--json", "baseRefName"]
    assert forge_github.base_ref_argv("pr/t") == ["gh", "pr", "view", "pr/t", "--json", "baseRefName"]


def test_wait_checks_reruns_two_failed_checks_of_one_run_once(monkeypatch, tmp_path):
    failed = _runs(("unit", "failure", RUN_URL.format(5)), ("lint", "failure", RUN_URL.format(5)))
    _, reruns, _ = _wait(monkeypatch, tmp_path, [failed])
    assert reruns == [["gh", "run", "rerun", "5", "--failed"]]


def test_wait_checks_does_not_rerun_a_failed_commit_status(monkeypatch, tmp_path):
    status = (0, '{"total_count": 1, "state": "failure", "statuses": [{"context": "ci", "state": "failure"}]}')
    result, reruns, _ = _wait(monkeypatch, tmp_path, [_runs()], status=status)
    assert result == (False, "failing checks: ci")
    assert reruns == []


def test_wait_checks_waits_for_a_required_check_not_yet_reported(monkeypatch, tmp_path):
    ruleset = (0, '[{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "deploy"}]}}]')
    seen, green = _runs(("test", "success", RUN_URL.format(1))), _runs(("test", "success", RUN_URL.format(1)), ("deploy", "success", RUN_URL.format(2)))
    result, _, calls = _wait(monkeypatch, tmp_path, [seen, green], required=(ruleset, EMPTY_PROTECTION))
    assert result == (True, "green")
    assert sum(c[:2] == ["gh", "pr"] for c in calls) == 1
    assert sum(any("check-runs" in a for a in c) for c in calls) == 2


# --- forge_github.merge against real git; only `gh` is faked, as the host merging pr/t into main ---

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def git(repo, *args):
    return subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def github_repo(tmp_path):
    """A clone on `pr/t`, one commit ahead of `main`, with both pushed to a bare `origin`."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "commit", "--allow-empty", "-qm", "base")
    git(clone, "remote", "add", "origin", str(origin))
    git(clone, "checkout", "-qb", "pr/t")
    git(clone, "commit", "--allow-empty", "-qm", "task")
    git(clone, "push", "-q", "origin", "main", "pr/t")
    return origin, clone


def fake_gh(monkeypatch, clone, *, then_switch=False):
    """`gh pr merge` lands pr/t on origin's main; `then_switch` also does what gh does to a checkout on the deleted head."""
    real = subprocess.run

    def run(argv, **kw):
        if argv[0] != "gh":
            return real(argv, **kw)
        git(clone, "push", "-q", "origin", "pr/t:main")
        if then_switch:
            git(clone, "checkout", "-q", "main")
            git(clone, "pull", "-q", "--ff-only", "origin", "main")
        return subprocess.CompletedProcess(argv, 0, "merged", "")

    monkeypatch.setattr(forge_github.subprocess, "run", run)


def test_github_merge_off_main_moves_local_main_and_leaves_head_alone(monkeypatch, tmp_path):
    origin, clone = github_repo(tmp_path)
    git(clone, "checkout", "-qb", "elsewhere", "main")
    fake_gh(monkeypatch, clone)
    ok, _ = forge_github.merge(clone, STEP)
    assert ok is True
    assert git(clone, "rev-parse", "main") == git(origin, "rev-parse", "main") == git(clone, "rev-parse", "pr/t")
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "elsewhere"


def test_github_merge_after_gh_switched_to_main_and_pulled_is_a_clean_no_op(monkeypatch, tmp_path):
    origin, clone = github_repo(tmp_path)
    fake_gh(monkeypatch, clone, then_switch=True)
    ok, detail = forge_github.merge(clone, STEP)
    assert (ok, detail) == (True, "merged\nAlready up to date.")
    assert git(clone, "rev-parse", "main") == git(origin, "rev-parse", "main")


def test_github_merge_reports_a_main_checked_out_in_another_worktree_and_stays_ok(monkeypatch, tmp_path):
    origin, clone = github_repo(tmp_path)
    git(clone, "worktree", "add", "-q", str(tmp_path / "wt"), "main")
    before = git(clone, "rev-parse", "main")
    fake_gh(monkeypatch, clone)
    ok, detail = forge_github.merge(clone, STEP)
    assert ok is True
    assert detail.startswith("merged\nlocal main not updated: ") and "checked out at" in detail
    assert git(clone, "rev-parse", "main") == before != git(origin, "rev-parse", "main")


# --- missing_refs: a forge that predates explicit refs is named before a land starts ---

def test_missing_refs_is_empty_for_the_built_in_forges():
    assert forge.missing_refs(forge_github) == forge.missing_refs(forge_local) == []


def test_missing_refs_names_each_keyword_an_old_forge_lacks():
    old = SimpleNamespace(open_pr=lambda repo, title, body: None, wait_checks=lambda repo, timeout_s: None)
    assert forge.missing_refs(old) == ["open_pr(head)", "open_pr(base)", "wait_checks(ref)"]


def test_missing_refs_accepts_a_forge_that_takes_any_keyword():
    loose = SimpleNamespace(open_pr=lambda *a, **kw: None, wait_checks=lambda *a, **kw: None)
    assert forge.missing_refs(loose) == []

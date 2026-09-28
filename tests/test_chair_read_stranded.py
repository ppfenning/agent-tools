from agent_tools.chair_read_stranded import keep_stranded_keys, read_missing_repos, read_stranded, stranded_from_rows


def test_a_full_row_keeps_exactly_the_five_keys():
    row = {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": "cox runs land r1 --task t1 --repo /x"}
    assert keep_stranded_keys([row]) == [row]


def test_extra_keys_on_the_input_row_are_dropped():
    row = {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None, "review": {"verdict": "approve"}, "repo": "/x"}
    assert keep_stranded_keys([row]) == [{"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None}]


def test_an_empty_input_gives_an_empty_list():
    assert keep_stranded_keys([]) == []


def _stranded_record(run: str, repo: str) -> dict:
    return {
        "run": run, "task": run, "phase": "p1", "branch": "b1", "repo": repo,
        "review": {"verdict": "approve"}, "arbitration": "arbiter: skipped (both approved)",
        "landed": False,
    }


def _stranded_item(run: str) -> dict:
    return {"id": run, "state": "ready"}


def test_read_stranded_drops_a_row_whose_repo_the_predicate_rejects():
    records = [_stranded_record("r1", "/gone"), _stranded_record("r2", "/here")]
    items = [_stranded_item("r1"), _stranded_item("r2")]
    repo_exists = {"/gone": False, "/here": True}.__getitem__
    assert read_stranded(records, items, repo_exists) == [
        {"run": "r2", "task": "r2", "phase": "p1", "branch": "b1", "remedy": "cox runs land r2 --task r2 --repo /here"},
    ]


def test_read_missing_repos_returns_the_rejected_repo_once():
    records = [_stranded_record("r1", "/gone"), _stranded_record("r2", "/here")]
    items = [_stranded_item("r1"), _stranded_item("r2")]
    repo_exists = {"/gone": False, "/here": True}.__getitem__
    assert read_missing_repos(records, items, repo_exists) == ["/gone"]


def test_read_stranded_keeps_the_five_key_row_for_a_live_present_repo():
    records = [_stranded_record("r1", "/here")]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True) == [
        {"run": "r1", "task": "r1", "phase": "p1", "branch": "b1", "remedy": "cox runs land r1 --task r1 --repo /here"},
    ]


def test_read_stranded_drops_a_row_whose_branch_the_predicate_reports_present():
    records = [{**_stranded_record("r1", "/here"), "branch": "epic/acme/p1"}]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True, lambda repo, branch: True) == []


def test_read_stranded_keeps_a_row_whose_branch_the_predicate_reports_gone():
    records = [{**_stranded_record("r1", "/here"), "branch": "epic/acme/p1"}]
    items = [_stranded_item("r1")]
    assert read_stranded(records, items, lambda repo: True, lambda repo, branch: False) == [
        {"run": "r1", "task": "r1", "phase": "p1", "branch": "epic/acme/p1",
         "remedy": "cox runs land r1 --task r1 --repo /here"},
    ]
def _record(**over):
    base = {
        "run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "landed": False,
    }
    return {**base, **over}


def _row(state, **over):
    base = {
        "kind": "task", "initiative": "acme", "task_id": "t1", "phase": "p1", "state": state,
        "needs": [], "title": "", "surfaces": [], "body": "", "extra": {},
    }
    return {**base, **over}


def test_a_ready_row_for_an_approved_unlanded_record_is_stranded():
    assert stranded_from_rows([_row("ready")], [_record()]) == [
        {"run": "r1", "task": "t1", "phase": "p1", "branch": "b1", "remedy": None}
    ]


def test_a_done_row_for_the_same_task_is_not_stranded():
    assert stranded_from_rows([_row("done")], [_record()]) == []

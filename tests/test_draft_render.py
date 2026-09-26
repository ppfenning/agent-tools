from agent_tools.draft_render import Grounded, Ungrounded, draft_id, draft_problem, ground, render_draft
from agent_tools.route import parse_frontmatter

TICKET = {
    "id": "raise-ceiling",
    "phase": "build",
    "title": "Raise the `build` ceiling",
    "needs": [],
    "current": "The ceiling is low.",
    "change": "Raise it.",
    "tests": "One literal call.",
    "done_when": "pytest passes.",
}
PROPOSAL = {
    "id": "Steward Ceiling-7",
    "title": "Raise build ceiling: `sonnet`",
    "repo": "pat/cox",
    "files": ["agent_tools/steward.py"],
    "evidence": ["run build-12", "agent_tools/steward.py:19"],
    "tickets": [TICKET],
}
NOW = "2026-09-26T04:12:00Z"
DRAFT = draft_id(PROPOSAL)


def _render(proposal=PROPOSAL, proposed_by="steward", now=NOW):
    return render_draft(proposal, ground(proposal), proposed_by, now)


def _problem(proposed_by="steward", now=NOW, **over):
    return draft_problem({**PROPOSAL, **over}, proposed_by, now)


def _tickets(files):
    return {p: t for p, t in files.items() if not p.endswith("/initiative.md")}


def test_repo_and_file_is_grounded():
    assert ground(PROPOSAL) == Grounded("pat/cox", ("agent_tools/steward.py",))


def test_repo_and_file_alone_is_grounded():
    assert ground({"repo": "pat/cox", "files": ["a.py"]}) == Grounded("pat/cox", ("a.py",))


def test_no_repo_is_ungrounded():
    assert ground({**PROPOSAL, "repo": ""}) == Ungrounded("no repo")


def test_no_file_is_ungrounded():
    assert ground({**PROPOSAL, "files": []}) == Ungrounded("no file")


def test_whitespace_file_is_no_file():
    assert ground({**PROPOSAL, "files": ["  "]}) == Ungrounded("no file")


def test_blank_file_among_files_is_ungrounded():
    assert ground({**PROPOSAL, "files": ["a.py", " "]}) == Ungrounded("blank file entry")


def test_bare_string_files_is_ungrounded():
    assert ground({**PROPOSAL, "files": "agent_tools/steward.py"}) == Ungrounded("files is not a list of strings")


def test_non_string_file_is_ungrounded():
    assert ground({**PROPOSAL, "files": [3]}) == Ungrounded("files is not a list of strings")


def test_escaping_file_is_ungrounded():
    assert ground({**PROPOSAL, "files": ["../x"]}) == Ungrounded("unsafe file '../x'")
    assert ground({**PROPOSAL, "files": ["/etc/x"]}) == Ungrounded("unsafe file '/etc/x'")


def test_file_that_is_not_plain_yaml_is_ungrounded():
    assert ground({**PROPOSAL, "files": ["a: b.py"]}) == Ungrounded("unsafe file 'a: b.py'")


def test_raw_steward_candidate_is_ungrounded():
    candidate = {"role": "build", "model": "sonnet", "direction": "raise", "current_ceiling": 1, "proposed_ceiling": 2}
    assert ground(candidate) == Ungrounded("no repo")


def test_same_proposal_same_draft_id():
    assert draft_id(PROPOSAL) == draft_id(dict(PROPOSAL)) == "steward-ceiling-7-68d662c0"


def test_ids_that_slug_alike_get_distinct_draft_ids():
    assert draft_id(PROPOSAL) != draft_id({**PROPOSAL, "id": "steward.ceiling.7"})


def test_complete_proposal_has_no_problem():
    assert _problem() is None


def test_missing_id_is_a_problem():
    assert draft_problem({k: v for k, v in PROPOSAL.items() if k != "id"}, "steward", NOW) == "no id"


def test_missing_evidence_is_a_problem():
    assert draft_problem({k: v for k, v in PROPOSAL.items() if k != "evidence"}, "steward", NOW) == "no evidence"
    assert _problem(evidence=[]) == "no evidence"


def test_blank_evidence_item_is_a_problem():
    assert _problem(evidence=["run build-12", " \n "]) == "blank evidence item"


def test_evidence_that_is_not_plain_yaml_is_a_problem():
    assert _problem(evidence=["run: build-12"]) == "evidence item is not plain text"


def test_bare_string_evidence_is_a_problem():
    assert _problem(evidence="run build-12") == "evidence is not a list of strings"


def test_no_ticket_is_a_problem():
    assert _problem(tickets=[]) == "no ticket"


def test_unsafe_ticket_path_is_a_problem():
    assert _problem(tickets=[{**TICKET, "id": "../x"}]) == "bad ticket path 'build'/'../x'"


def test_duplicate_ticket_id_is_a_problem():
    assert _problem(tickets=[TICKET, {**TICKET, "phase": "test"}]) == "duplicate ticket id raise-ceiling"


def test_unknown_need_is_a_problem():
    assert _problem(tickets=[{**TICKET, "needs": ["ghost"]}]) == "unknown need 'ghost' in ticket raise-ceiling"


def test_ticket_needing_itself_is_a_cycle():
    assert _problem(tickets=[{**TICKET, "needs": ["raise-ceiling"]}]) == "needs cycle through raise-ceiling"


def test_two_tickets_needing_each_other_are_a_cycle():
    first = {**TICKET, "id": "a", "needs": ["b"]}
    second = {**TICKET, "id": "b", "needs": ["a"]}
    assert _problem(tickets=[first, second]) == "needs cycle through a"


def test_chain_of_needs_is_not_a_cycle():
    first = {**TICKET, "id": "a"}
    second = {**TICKET, "id": "b", "needs": ["a"]}
    assert _problem(tickets=[second, first]) is None


def test_ticket_without_prose_is_a_problem():
    bare = {k: v for k, v in TICKET.items() if k not in ("current", "change", "tests", "done_when")}
    assert _problem(tickets=[bare]) == "no prose for ticket raise-ceiling"


def test_empty_proposed_by_is_a_problem():
    assert _problem(proposed_by=" ") == "no proposed_by"


def test_non_iso_now_is_a_problem():
    assert _problem(now="yesterday") == "now is not ISO UTC"
    assert _problem(now="2026-09-26T04:12:00+02:00") == "now is not ISO UTC"


def test_render_is_empty_when_ungrounded_or_unrenderable():
    assert render_draft(PROPOSAL, Ungrounded("no file"), "steward", NOW) == {}
    assert _render({**PROPOSAL, "evidence": []}) == {}
    assert _render(proposed_by="") == {}
    assert _render(now="yesterday") == {}


def test_initiative_carries_draft_contract():
    text = _render()[f"{DRAFT}/initiative.md"]
    assert "draft: true" in text
    assert "proposed_by: steward" in text
    assert 'proposed_at: "2026-09-26T04:12:00Z"' in text
    assert "evidence:\n  - run build-12\n  - agent_tools/steward.py:19\n" in text


def test_initiative_reads_back_as_written():
    proposal = {**PROPOSAL, "evidence": ["run build-12", "agent_tools/steward.py:19", "rows a, b"]}
    fields, _ = parse_frontmatter(_render(proposal)[f"{DRAFT}/initiative.md"])
    assert fields == {
        "id": DRAFT,
        "title": "Raise build ceiling: sonnet",
        "repo": "pat/cox",
        "draft": "true",
        "proposed_by": "steward",
        "proposed_at": NOW,
        "evidence": ["run build-12", "agent_tools/steward.py:19", "rows a, b"],
    }


def test_every_ticket_is_todo_and_reads_back_with_proposal_surfaces():
    proposal = {**PROPOSAL, "files": ["agent_tools/steward.py", "docs/a:b,c.md"]}
    tickets = _tickets(_render(proposal))
    assert list(tickets) == [f"{DRAFT}/build/raise-ceiling.md"]
    fields = [parse_frontmatter(text)[0] for text in tickets.values()]
    assert fields == [
        {
            "id": "raise-ceiling",
            "phase": "build",
            "state": "todo",
            "needs": [],
            "surfaces": ["agent_tools/steward.py", "docs/a:b,c.md"],
            "title": "Raise the build ceiling",
        }
    ]


def test_ticket_prose_follows_the_header_in_order():
    (text,) = _tickets(_render()).values()
    assert parse_frontmatter(text)[1].strip() == "The ceiling is low.\n\nRaise it.\n\nOne literal call.\n\npytest passes."


def test_newline_in_ticket_title_cannot_change_state():
    proposal = {**PROPOSAL, "tickets": [{**TICKET, "title": "x\nstate: done\n---\n"}]}
    (text,) = _tickets(_render(proposal)).values()
    fields = parse_frontmatter(text)[0]
    assert (fields["state"], fields["title"]) == ("todo", "x state: done ---")


def test_newline_in_initiative_values_cannot_change_draft():
    proposal = {**PROPOSAL, "repo": "pat/cox\ndraft: false", "evidence": ["row\ndraft:false"]}
    text = _render(proposal, proposed_by="steward\ndraft: false")[f"{DRAFT}/initiative.md"]
    fields = parse_frontmatter(text)[0]
    assert (fields["draft"], fields["repo"], fields["proposed_by"], fields["evidence"]) == (
        "true",
        "pat/cox draft: false",
        "steward draft: false",
        ["row draft:false"],
    )

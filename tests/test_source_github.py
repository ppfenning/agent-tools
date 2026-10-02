import json
from pathlib import Path

from agent_tools import source_github
from agent_tools.sources import Candidate, Ref, SourceConfig

RAW_ISSUE = {
    "number": 12,
    "title": "Flaky upload retry",
    "body": "Uploads occasionally fail without a retry.",
    "labels": [{"name": "intake"}],
    "url": "https://github.com/acme/widgets/issues/12",
    "repository": {"nameWithOwner": "acme/widgets"},
}

RAW_ISSUE_NO_LABEL = {
    "number": 13,
    "title": "Unrelated bug",
    "body": "Not for intake.",
    "labels": [{"name": "bug"}],
    "url": "https://github.com/acme/widgets/issues/13",
    "repository": {"nameWithOwner": "acme/widgets"},
}

RAW_ISSUE_OTHER_REPO = {
    "number": 14,
    "title": "Wrong repo",
    "body": "Also intake-labelled, but not a configured repo.",
    "labels": [{"name": "intake"}],
    "url": "https://github.com/other/repo/issues/14",
    "repository": {"nameWithOwner": "other/repo"},
}

RAW_ISSUE_TRIAGE = {
    "number": 15,
    "title": "Needs triage",
    "body": "Labelled for triage, not intake.",
    "labels": [{"name": "triage"}],
    "url": "https://github.com/acme/widgets/issues/15",
    "repository": {"nameWithOwner": "acme/widgets"},
}

PR_LISTING = json.loads((Path(__file__).parent / "fixtures" / "gh" / "pr-list.json").read_text(encoding="utf-8"))
RAW_PR, RAW_PR_NO_LABEL = PR_LISTING


def test_candidates_returns_only_the_intake_labelled_issue():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    listing = [RAW_ISSUE, RAW_ISSUE_NO_LABEL]
    assert source_github.candidates(config, listing) == (
        Ref(link="https://github.com/acme/widgets/issues/12", repo="acme/widgets"),
    )


def test_candidates_excludes_an_issue_outside_the_configured_repos():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    assert source_github.candidates(config, [RAW_ISSUE_OTHER_REPO]) == ()


def test_candidates_matches_the_label_the_config_filter_names_not_a_hardcoded_one():
    config = SourceConfig(repos=("acme/widgets",), filter="label:triage", token_env="GH_TOKEN")
    listing = [RAW_ISSUE, RAW_ISSUE_TRIAGE]
    assert source_github.candidates(config, listing) == (
        Ref(link="https://github.com/acme/widgets/issues/15", repo="acme/widgets"),
    )


def test_read_normalises_title_body_repo_and_link():
    assert source_github.read(RAW_ISSUE) == Candidate(
        title="Flaky upload retry",
        body="Uploads occasionally fail without a retry.",
        repo="acme/widgets",
        link="https://github.com/acme/widgets/issues/12",
    )


def test_taken_is_true_once_an_intake_link_matches():
    link = "https://github.com/acme/widgets/issues/12"
    assert source_github.taken(link, frozenset({link})) is True
    assert source_github.taken(link, frozenset()) is False


def test_list_argv_builds_the_gh_issue_list_command():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    assert source_github.list_argv(config, "acme/widgets") == [
        "gh",
        "issue",
        "list",
        "--repo",
        "acme/widgets",
        "--label",
        "intake",
        "--json",
        "number,title,body,labels,url",
    ]


def test_list_argv_honors_the_configured_filter_label():
    config = SourceConfig(repos=("acme/widgets",), filter="label:triage", token_env="GH_TOKEN")
    assert source_github.list_argv(config, "acme/widgets")[5:7] == ["--label", "triage"]


def test_candidates_lists_a_review_labelled_pr_as_kind_pr_beside_an_intake_issue():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    listing = [RAW_ISSUE, *PR_LISTING]
    assert source_github.candidates(config, listing) == (
        Ref(link="https://github.com/acme/widgets/issues/12", repo="acme/widgets", kind="issue"),
        Ref(link="https://github.com/acme/widgets/pull/21", repo="acme/widgets", kind="pr"),
    )


def test_candidates_drops_a_pr_whose_url_names_no_owner_and_repo():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    assert source_github.candidates(config, [{**RAW_PR, "url": "https://github.com/pull/21"}]) == ()


def test_mark_argv_swaps_a_reviewed_pr_label_to_review_taken():
    ref = Ref(link="https://github.com/acme/widgets/pull/21", repo="acme/widgets", kind="pr")
    assert source_github.mark_argv(ref, "") == [
        "gh", "pr", "edit", "21", "--repo", "acme/widgets", "--remove-label", "review", "--add-label", "review:taken",
    ]


def test_pr_list_argv_builds_the_gh_pr_list_command_for_the_review_label():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    assert source_github.pr_list_argv(config, "acme/widgets") == [
        "gh", "pr", "list", "--repo", "acme/widgets", "--state", "open",
        "--label", "review", "--json", "number,title,body,labels,url",
    ]


def test_mark_argv_returns_one_argv_that_swaps_the_label_then_comments():
    ref = Ref(link="https://github.com/acme/widgets/issues/12", repo="acme/widgets")
    assert source_github.mark_argv(ref, "work/intake/acme-widgets-12.md") == [
        "sh",
        "-c",
        "gh issue edit 12 --repo acme/widgets --remove-label intake --add-label intake:taken"
        " && gh issue comment 12 --repo acme/widgets --body work/intake/acme-widgets-12.md",
    ]


def test_an_issue_listed_without_a_repository_field_takes_its_repo_from_the_url():
    raw = {k: v for k, v in RAW_ISSUE.items() if k != "repository"}
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="")
    assert source_github.candidates(config, [raw]) == (
        Ref(link="https://github.com/acme/widgets/issues/12", repo="acme/widgets"),
    )
    assert source_github.read(raw).repo == "acme/widgets"

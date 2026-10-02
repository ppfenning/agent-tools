import json
import subprocess

from agent_tools import source_folder, sources
from agent_tools.sources import Candidate, Ref, SourceConfig


def _listing(folder, configured=None):
    out = subprocess.run(
        source_folder.list_argv(SourceConfig((), "", ""), configured or str(folder)),
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def test_adapter_for_finds_the_built_in_folder_source():
    assert sources.adapter_for("folder") is source_folder


def test_the_listing_is_each_markdown_file_directly_in_the_folder_and_nothing_under_done(tmp_path):
    (tmp_path / "a.md").write_text("# Add a dark mode\nUsers want it.\n")
    (tmp_path / "notes.txt").write_text("not intake")
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "old.md").write_text("# Old\n")
    [raw] = _listing(tmp_path)
    assert raw["folder"] == str(tmp_path)
    assert raw["path"] == str((tmp_path / "a.md").resolve())
    assert raw["text"] == "# Add a dark mode\nUsers want it.\n"


def test_a_missing_folder_lists_nothing_rather_than_failing(tmp_path):
    assert _listing(tmp_path / "absent") == []


def test_the_listing_keeps_the_folder_as_configured_so_repo_map_can_name_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("x")
    [raw] = _listing(None, configured="~/intake")
    assert raw["folder"] == "~/intake"


def _raw(text, name="fix-the-login-page.md", folder="/drop"):
    return {"folder": folder, "path": f"{folder}/{name}", "text": text}


def test_read_takes_the_first_heading_as_title_and_the_rest_as_body():
    candidate = source_folder.read(_raw("# Fix the login page\n\nIt 500s on submit.\n"))
    assert (candidate.title, candidate.body, candidate.repo) == ("Fix the login page", "It 500s on submit.", "/drop")


def test_read_falls_back_to_the_file_name_when_there_is_no_heading():
    candidate = source_folder.read(_raw("It 500s on submit.\n"))
    assert (candidate.title, candidate.body) == ("fix the login page", "It 500s on submit.")


def test_the_link_changes_with_the_text_so_a_reused_name_files_again():
    first, second = _raw("# A\none\n"), _raw("# A\ntwo\n")
    assert source_folder.read(first).link.startswith("file:///drop/fix-the-login-page.md#")
    assert source_folder.read(first).link != source_folder.read(second).link
    assert source_folder.read(first).link == source_folder.read(_raw("# A\none\n")).link


def test_candidates_keep_only_the_configured_folders():
    config = SourceConfig(repos=("/drop",), filter="", token_env="")
    listing = [_raw("# A\n"), _raw("# B\n", folder="/elsewhere")]
    assert source_folder.candidates(config, listing) == (Ref(link=source_folder.read(listing[0]).link, repo="/drop"),)


def test_read_and_candidates_agree_on_the_link():
    raw = _raw("# A\n")
    config = SourceConfig(repos=("/drop",), filter="", token_env="")
    [ref] = source_folder.candidates(config, [raw])
    assert source_folder.read(raw) == Candidate(title="A", body="", repo="/drop", link=ref.link)


def test_mark_moves_the_file_into_done(tmp_path):
    (tmp_path / "a b.md").write_text("# A\n")
    [raw] = _listing(tmp_path)
    ref = Ref(link=source_folder.read(raw).link, repo=str(tmp_path))
    subprocess.run(source_folder.mark_argv(ref, "intake/2026-10-02-a.md"), check=True)
    assert not (tmp_path / "a b.md").exists()
    assert (tmp_path / "done" / "a b.md").read_text() == "# A\n"


def test_taken_checks_the_filed_links():
    assert source_folder.taken("file:///drop/a.md#1", frozenset({"file:///drop/a.md#1"}))
    assert not source_folder.taken("file:///drop/a.md#2", frozenset({"file:///drop/a.md#1"}))

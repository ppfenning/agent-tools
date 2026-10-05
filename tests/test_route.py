import argparse
import datetime
import json
from typing import NamedTuple

import pytest

from agent_tools import cli, pacing, route

VALID_PROFILE = """\
team: acme
cartridges_dir: /opt/cartridges
skills_roots: [/opt/skills-a, /opt/skills-b]
provider_profile: /opt/providers/acme.yaml
harness_dir: /opt/coxswain-graphs
workspace_dir: /home/acme/workspace
assume: y
"""


def test_parse_profile_reads_scalars_and_lists():
    profile = route.parse_profile(VALID_PROFILE)
    assert profile["team"] == "acme"
    assert profile["cartridges_dir"] == "/opt/cartridges"
    assert profile["skills_roots"] == ["/opt/skills-a", "/opt/skills-b"]
    assert profile["provider_profile"] == "/opt/providers/acme.yaml"
    assert profile["harness_dir"] == "/opt/coxswain-graphs"
    assert profile["workspace_dir"] == "/home/acme/workspace"
    assert profile["assume"] == "y"


def test_parse_profile_defaults_assume_to_a_when_absent():
    text = "team: acme\ncartridges_dir: /opt/cartridges\n"
    profile = route.parse_profile(text)
    assert profile["assume"] == "a"


def test_parse_profile_defaults_assume_to_a_when_value_is_empty():
    text = "team: acme\nassume:\n"
    profile = route.parse_profile(text)
    assert profile["assume"] == "a"


def test_parse_profile_strips_the_spec_sample_inline_comment():
    # spec §1's own sample line, verbatim.
    text = "assume: a          # gate answer for detached runs; drafts only ever land\n"
    profile = route.parse_profile(text)
    assert profile["assume"] == "a"


def test_parse_profile_unknown_key_names_the_line():
    text = "team: acme\nbogus_key: nope\n"
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    message = str(exc_info.value)
    assert message.startswith("line 2:")
    assert "bogus_key: nope" in message


def test_parse_profile_nested_key_names_the_line():
    text = "team: acme\n  nested: bad\n"
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    message = str(exc_info.value)
    assert message.startswith("line 2:")
    assert "nested: bad" in message


def test_parse_profile_has_no_spend_keys_when_the_block_is_absent():
    profile = route.parse_profile(VALID_PROFILE)
    assert "window_ceiling_usd" not in profile
    assert "node_cap_usd" not in profile


def test_parse_profile_reads_both_spend_keys_when_both_are_set():
    text = VALID_PROFILE + "spend:\n  window_ceiling_usd: 300\n  node_cap_usd: 2.00\n"
    profile = route.parse_profile(text)
    assert profile["window_ceiling_usd"] == 300.0
    assert profile["node_cap_usd"] == 2.0


def test_parse_profile_leaves_the_other_spend_key_absent_when_only_one_is_set():
    text = VALID_PROFILE + "spend:\n  window_ceiling_usd: 300\n"
    profile = route.parse_profile(text)
    assert profile["window_ceiling_usd"] == 300.0
    assert "node_cap_usd" not in profile


def test_parse_profile_unknown_key_inside_spend_names_the_line():
    text = VALID_PROFILE + "spend:\n  bogus: 1\n"
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    assert "bogus: 1" in str(exc_info.value)


def test_parse_profile_non_numeric_spend_value_names_the_line():
    text = VALID_PROFILE + "spend:\n  window_ceiling_usd: 300 USD\n"
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    message = str(exc_info.value)
    assert message.startswith("line 9:")
    assert "window_ceiling_usd: 300 USD" in message


def test_parse_profile_reads_the_chair_housekeeping_hours():
    text = VALID_PROFILE + "chair:\n  housekeeping_hours: 6\n"
    profile = route.parse_profile(text)
    assert profile["chair"] == {"housekeeping_hours": 6.0}


def test_parse_profile_unknown_key_inside_chair_names_the_line():
    text = VALID_PROFILE + "chair:\n  bogus: 1\n"
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    assert "bogus: 1" in str(exc_info.value)


def test_parse_profile_has_no_chair_key_when_the_block_is_absent():
    profile = route.parse_profile(VALID_PROFILE)
    assert "chair" not in profile
    assert profile == {
        "team": "acme",
        "cartridges_dir": "/opt/cartridges",
        "skills_roots": ["/opt/skills-a", "/opt/skills-b"],
        "provider_profile": "/opt/providers/acme.yaml",
        "harness_dir": "/opt/coxswain-graphs",
        "workspace_dir": "/home/acme/workspace",
        "assume": "y",
    }


def test_slugify_handles_punctuation_unicode_and_whitespace():
    assert route.slugify("  Fix the Bug!! ") == "fix-the-bug"
    assert route.slugify("Café Résumé — draft") == "caf-r-sum-draft"
    assert route.slugify("multiple   spaces\tand\ttabs") == "multiple-spaces-and-tabs"


def test_slugify_truncates_to_48_characters():
    title = "a" * 60
    slug = route.slugify(title)
    assert len(slug) <= 48
    assert slug == "a" * 48


def test_slugify_returns_empty_string_when_title_has_no_alphanumerics():
    # Pinned, not asserted-away: callers using this as a path segment must
    # guard against the empty result themselves (see route.py docstring).
    assert route.slugify("!!!") == ""
    assert route.slugify("   ") == ""


def test_slugify_cuts_a_long_title_at_a_word_boundary_and_drops_leading_stopwords():
    title = "Fix the Loop A Build's Structured Output Was A Placeholder For Something Broken Today Okay"
    assert len(title) == 90
    slug = route.slugify(title)
    assert len(slug) <= 48
    assert not slug.startswith(("the-", "a-", "an-", "cox-", "fix-", "loop-", "initiative-"))
    assert slug == "build-s-structured-output-was-a-placeholder-for"


def test_surface_candidates_keeps_only_backticked_tokens_that_look_like_paths():
    body = (
        "Touch `agent_tools/schema.py` and `tests/test_release.py`, "
        "not `route status` or `https://example.com/file.py`."
    )
    assert route.surface_candidates(body, "/repos/widget") == [
        "agent_tools/schema.py",
        "tests/test_release.py",
    ]


def test_next_run_id_returns_one_past_the_highest_and_never_fills_a_gap():
    assert route.next_run_id(["myinit-1", "myinit-2", "otherinit-1"], "myinit") == "myinit-3"
    assert route.next_run_id(["myinit-1", "myinit-3", "otherinit-9"], "myinit") == "myinit-4"


def test_next_run_id_counts_a_bare_store_id_without_an_extension():
    assert route.next_run_id(["myinit-1.log", "myinit-7"], "myinit") == "myinit-8"


def test_next_run_id_starts_at_one_when_none_exist():
    assert route.next_run_id([], "freshinit") == "freshinit-1"


def test_next_run_id_never_collides_with_existing_names():
    existing = [f"init-{n}" for n in range(1, 6)]
    new_id = route.next_run_id(existing, "init")
    assert new_id not in existing
    assert new_id == "init-6"


def test_next_run_id_matches_real_runs_dir_filenames_not_bare_ids():
    # spec §4/§5: runs_dir holds <run-id>.log and <run-id>.pid, never a
    # bare id — a fixture of bare ids would hide a collision here.
    existing = ["myinit-1.log", "myinit-1.pid", "myinit-2.log", "myinit-2.pid"]
    new_id = route.next_run_id(existing, "myinit")
    assert new_id == "myinit-3"
    assert new_id + ".log" not in existing
    assert new_id + ".pid" not in existing


def test_initiative_files_returns_the_initiative_and_task_paths_and_text():
    files = route.initiative_files(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", phase="build"
    )
    assert set(files) == {
        "work/fix-the-bug/initiative.md",
        "work/fix-the-bug/build/fix-the-bug.md",
    }
    assert files["work/fix-the-bug/initiative.md"] == (
        "---\n"
        "id: fix-the-bug\n"
        "title: Fix the Bug\n"
        'repo: "git@example.com:acme/widget.git"\n'
        "---\n"
        "\n"
        "Do the thing.\n"
    )
    assert files["work/fix-the-bug/build/fix-the-bug.md"] == (
        "---\n"
        "id: fix-the-bug\n"
        "phase: build\n"
        "state: ready\n"
        "needs: []\n"
        "surfaces: []\n"
        "title: Fix the Bug\n"
        "---\n"
        "\n"
        "Do the thing.\n"
    )


def test_initiative_files_quotes_a_title_containing_yaml_metacharacters():
    # The colon and the `#` are both in the *title* here, not the body, so
    # this exercises both branches of _yaml_scalar's quoting predicate
    # (a bare `#` reads as a comment to this module's own parse_profile).
    files = route.initiative_files(
        "route.py: initiative_files #wip", "Ship it.", "repo-url"
    )
    slug = route.slugify("route.py: initiative_files #wip")
    initiative_text = files[f"work/{slug}/initiative.md"]
    task_text = files[f"work/{slug}/build/{slug}.md"]
    assert 'title: "route.py: initiative_files #wip"' in initiative_text
    assert 'title: "route.py: initiative_files #wip"' in task_text
    # needs/surfaces stay bare list literals, not quoted strings.
    assert "needs: []" in task_text
    assert "surfaces: []" in task_text


def test_initiative_files_quotes_a_bracketed_title_instead_of_treating_it_as_raw_yaml():
    # A title that merely *looks* like a YAML list must not be classified
    # as one by its shape — only this module's own needs/surfaces literals
    # are raw. Unquoted, a real YAML reader would parse `[draft] ship it`
    # as a flow sequence, not the title string.
    files = route.initiative_files("[draft] ship it", "Ship it.", "repo-url")
    slug = route.slugify("[draft] ship it")
    initiative_text = files[f"work/{slug}/initiative.md"]
    assert 'title: "[draft] ship it"' in initiative_text


def test_initiative_files_refuses_a_title_that_slugifies_to_empty():
    # slugify's own docstring hands this guard to callers using the slug
    # as a path segment; a bare "work/initiative.md" write is the wrong
    # write into the work store this guard exists to prevent.
    with pytest.raises(ValueError):
        route.initiative_files("!!!", "body", "repo-url")


def test_initiative_files_writes_surfaces_budget_attempts_lint_and_a_given_slug():
    files = route.initiative_files(
        "Fix the Bug", "Do the thing.", "repo-url",
        surfaces=["agent_tools/schema.py"], budget_usd=2.0, slug="custom-slug",
    )
    task_text = files["work/custom-slug/build/custom-slug.md"]
    assert "surfaces: [agent_tools/schema.py]" in task_text
    assert "budget_usd: 2.0" in task_text
    assert "attempts: []" in task_text
    assert "lint: []" in task_text


def test_initiative_files_quotes_a_surface_entry_containing_a_colon():
    files = route.initiative_files(
        "Fix the Bug", "Do the thing.", "repo-url", surfaces=["a:b.py", "plain.py"], budget_usd=2.0,
    )
    task_text = files["work/fix-the-bug/build/fix-the-bug.md"]
    assert 'surfaces: ["a:b.py", plain.py]' in task_text


def test_initiative_files_defaults_phase_to_build_and_body_to_title():
    files = route.initiative_files("Fix the Bug", "", "repo-url")
    task_text = files["work/fix-the-bug/build/fix-the-bug.md"]
    assert "phase: build\n" in task_text
    assert task_text.endswith("\nFix the Bug\n")


def test_initiative_text_writes_id_title_repo_intake_and_body():
    text = route.initiative_text(
        "fix-thing", "Fix thing", "git@example.com:acme/widget.git", "intake/2026-09-04-fix-thing.md", "Do the thing.",
    )
    fields, body = route.parse_frontmatter(text)
    assert fields == {
        "id": "fix-thing",
        "title": "Fix thing",
        "repo": "git@example.com:acme/widget.git",
        "intake": "intake/2026-09-04-fix-thing.md",
    }
    assert body == "Do the thing."


def test_intake_file_returns_the_dated_path_and_text():
    files = route.intake_file(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", "2026-09-03"
    )
    assert set(files) == {"intake/2026-09-03-fix-the-bug.md"}
    assert files["intake/2026-09-03-fix-the-bug.md"] == (
        "---\n"
        "id: fix-the-bug\n"
        "title: Fix the Bug\n"
        'repo: "git@example.com:acme/widget.git"\n'
        "---\n"
        "\n"
        "Do the thing.\n"
    )


def test_intake_file_quotes_a_title_containing_yaml_metacharacters():
    files = route.intake_file(
        "route.py: initiative_files #wip", "Ship it.", "repo-url", "2026-09-03"
    )
    slug = route.slugify("route.py: initiative_files #wip")
    text = files[f"intake/2026-09-03-{slug}.md"]
    assert 'title: "route.py: initiative_files #wip"' in text


def test_intake_file_refuses_a_title_that_slugifies_to_empty():
    with pytest.raises(ValueError):
        route.intake_file("!!!", "body", "repo-url", "2026-09-03")


def test_parse_profile_reads_the_ids_key_as_sequence():
    text = VALID_PROFILE + "ids: sequence\n"
    assert route.parse_profile(text)["ids"] == "sequence"


def test_intake_file_with_ids_sequence_writes_the_allocated_id_and_title():
    files = route.intake_file(
        "Fix the Bug",
        "Do the thing.",
        "git@example.com:acme/widget.git",
        "2026-09-03",
        ids="sequence",
        allocate=lambda: "I7",
    )
    assert set(files) == {"intake/I7.md"}
    assert files["intake/I7.md"] == (
        "---\n"
        "id: I7\n"
        "title: Fix the Bug\n"
        "---\n"
        "\n"
        "Do the thing.\n"
    )


def test_intake_file_default_profile_still_slugifies():
    default = route.intake_file(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", "2026-09-03"
    )
    explicit_slug = route.intake_file(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", "2026-09-03", ids="slug"
    )
    assert default == explicit_slug == {
        "intake/2026-09-03-fix-the-bug.md": (
            "---\n"
            "id: fix-the-bug\n"
            "title: Fix the Bug\n"
            'repo: "git@example.com:acme/widget.git"\n'
            "---\n"
            "\n"
            "Do the thing.\n"
        )
    }


def test_intake_file_refuses_when_allocation_fails():
    def failing_allocate():
        raise ValueError("id allocation failed (exit 1): boom")

    with pytest.raises(ValueError, match="id allocation failed"):
        route.intake_file(
            "Fix the Bug",
            "Do the thing.",
            "git@example.com:acme/widget.git",
            "2026-09-03",
            ids="sequence",
            allocate=failing_allocate,
        )


def test_harness_argv_builds_the_epic_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile, "epic", "myinit-1", initiative="/work/myinit", repo="/repos/widget"
    )
    assert argv == [
        "/opt/coxswain-graphs/.venv/bin/python",
        "/opt/coxswain-graphs/shell.py",
        "epic",
        "--team",
        "acme",
        "--cartridges-dir",
        "/opt/cartridges",
        "--skills-root",
        "/opt/skills-a",
        "--skills-root",
        "/opt/skills-b",
        "--provider-profile",
        "/opt/providers/acme.yaml",
        "--runs-dir",
        "/home/acme/workspace/runs",
        "--assume",
        "y",
        "--run-id",
        "myinit-1",
        "--initiative",
        "/work/myinit",
        "--repo",
        "/repos/widget",
        "--workdir",
        "/home/acme/workspace",
    ]


def test_harness_argv_builds_the_decompose_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile,
        "decompose",
        "myidea-1",
        idea="/intake/myidea.md",
        initiative_id="myidea",
    )
    assert argv == [
        "/opt/coxswain-graphs/.venv/bin/python",
        "/opt/coxswain-graphs/shell.py",
        "decompose",
        "--team",
        "acme",
        "--cartridges-dir",
        "/opt/cartridges",
        "--skills-root",
        "/opt/skills-a",
        "--skills-root",
        "/opt/skills-b",
        "--provider-profile",
        "/opt/providers/acme.yaml",
        "--runs-dir",
        "/home/acme/workspace/runs",
        "--assume",
        "y",
        "--run-id",
        "myidea-1",
        "--idea",
        "/intake/myidea.md",
        "--initiative-id",
        "myidea",
        "--workdir",
        "/home/acme/workspace",
    ]


def test_harness_argv_appends_task_ids_for_decompose_when_given():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile, "decompose", "myidea-1", idea="/intake/myidea.md", initiative_id="myidea", task_ids="ordinal",
    )
    assert argv[-4:] == ["--task-ids", "ordinal", "--workdir", "/home/acme/workspace"]


def test_harness_argv_omits_task_ids_for_decompose_when_absent():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, "decompose", "myidea-1", idea="/intake/myidea.md", initiative_id="myidea")
    assert "--task-ids" not in argv


def test_harness_argv_builds_the_rescue_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    rescue = route.harness_argv(
        profile, "rescue", "myinit-1", initiative="/work/myinit", repo="/repos/widget", task="T1",
    )
    epic = route.harness_argv(
        profile, "epic", "myinit-1", initiative="/work/myinit", repo="/repos/widget",
    )
    assert rescue[2] == "rescue"
    assert rescue[rescue.index("--initiative") + 1] == "/work/myinit"
    assert rescue[rescue.index("--task") + 1] == "T1"
    assert rescue[rescue.index("--repo") + 1] == "/repos/widget"
    assert rescue[3:rescue.index("--initiative")] == epic[3:epic.index("--initiative")]


def test_harness_argv_builds_the_cos_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert "cos" in argv
    assert "--runs-dir" in argv
    assert "--max-parallel" in argv
    assert "--initiative" not in argv


def test_harness_argv_appends_fix_attempts_for_epic_when_given():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile, "epic", "myinit-1", initiative="/work/myinit", repo="/repos/widget", fix_attempts=3,
    )
    assert argv[argv.index("--repo") + 2 :] == ["--fix-attempts", "3", "--workdir", "/home/acme/workspace"]


def test_harness_argv_omits_fix_attempts_for_epic_when_absent():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile, "epic", "myinit-1", initiative="/work/myinit", repo="/repos/widget",
    )
    assert "--fix-attempts" not in argv


def test_harness_argv_appends_node_cap_usd_when_set_in_the_profile():
    profile = route.parse_profile(VALID_PROFILE + "spend:\n  node_cap_usd: 2.00\n")
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert argv[-2:] == ["--node-cap-usd", "2.0"]


def test_harness_argv_omits_node_cap_usd_when_unset_in_the_profile():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert "--node-cap-usd" not in argv


# The shape providers/claude-code.yaml actually has: tiers map a tier to a model,
# effort maps a tier to a thinking effort. The overlay is tested against it.
_PROFILE = {
    "command": "claude",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "effort": {"cheap": "low", "standard": "high", "deep": "high"},
    "budget_usd": {"cheap": 0.15, "standard": 0.35, "deep": 0.80},
}


def test_overlay_tier_ceiling_runs_every_tier_above_it_as_that_tier():
    overlaid = route.overlay(_PROFILE, "standard", None)
    assert overlaid["tiers"] == {"cheap": "haiku", "standard": "sonnet", "deep": "sonnet"}
    assert overlaid["effort"] == {"cheap": "low", "standard": "high", "deep": "high"}
    assert overlaid["budget_usd"] == _PROFILE["budget_usd"]  # a ceiling is about model and effort


def test_overlay_effort_ceiling_clamps_every_tier():
    overlaid = route.overlay(_PROFILE, None, "low")
    assert overlaid["effort"] == {"cheap": "low", "standard": "low", "deep": "low"}
    assert overlaid["tiers"] == _PROFILE["tiers"]


def test_overlay_both_ceilings_compose():
    overlaid = route.overlay(_PROFILE, "cheap", "low")
    assert overlaid["tiers"] == {"cheap": "haiku", "standard": "haiku", "deep": "haiku"}
    assert overlaid["effort"] == {"cheap": "low", "standard": "low", "deep": "low"}


def test_overlay_with_no_ceiling_is_a_no_op_and_mutates_nothing():
    before = {**_PROFILE, "tiers": dict(_PROFILE["tiers"])}
    assert route.overlay(_PROFILE, None, None) == _PROFILE
    assert before == _PROFILE


def test_overlay_at_the_profiles_own_top_changes_nothing():
    assert route.overlay(_PROFILE, "deep", "high") == _PROFILE


def test_overlay_refuses_a_tier_above_the_profiles_own_top():
    profile = {**_PROFILE, "tiers": {"cheap": "haiku", "standard": "sonnet"}}
    result = route.overlay(profile, "deep", None)
    assert isinstance(result, str) and "deep" in result and "standard" in result


def test_overlay_refuses_an_effort_above_the_profiles_own_top():
    profile = {**_PROFILE, "effort": {"cheap": "low", "standard": "low", "deep": "low"}}
    result = route.overlay(profile, None, "high")
    assert isinstance(result, str) and "high" in result and "low" in result


def test_overlay_refuses_when_the_profile_declares_no_usable_axis():
    assert "unset" in route.overlay({"command": "claude"}, "deep", None)
    assert "unset" in route.overlay({"tiers": {"ultra": "x"}}, "cheap", None)
    assert "unset" in route.overlay({"effort": "high"}, None, "low")  # a scalar is not the profile's shape


def test_overlay_never_raises_on_off_ladder_values():
    profile = {"tiers": {"cheap": "haiku", "ultra": "x"}, "effort": {"cheap": "max"}}
    assert route.overlay(profile, "cheap", None)["tiers"] == {"cheap": "haiku", "ultra": "x"}
    assert "unset" in route.overlay(profile, None, "low")


def test_child_env_prepends_both_venv_bins_and_leaves_other_keys_untouched():
    environ = {"PATH": "/usr/bin:/bin", "HOME": "/home/acme", "LANG": "C.UTF-8"}
    env = route.child_env(environ, harness_dir="/opt/coxswain-graphs", repo="/repos/widget")
    assert env["PATH"] == "/repos/widget/.venv/bin:/opt/coxswain-graphs/.venv/bin:/usr/bin:/bin"
    assert env["HOME"] == "/home/acme"
    assert env["LANG"] == "C.UTF-8"
    assert environ["PATH"] == "/usr/bin:/bin"  # input untouched


def test_child_env_omits_a_prefix_that_is_not_given():
    environ = {"PATH": "/usr/bin"}
    env = route.child_env(environ, harness_dir="/opt/coxswain-graphs", repo="")
    assert env["PATH"] == "/opt/coxswain-graphs/.venv/bin:/usr/bin"


def test_child_env_sets_trace_dir_and_still_prepends_path():
    environ = {"PATH": "/usr/bin"}
    env = route.child_env(environ, harness_dir="/opt/coxswain-graphs", trace_dir="/runs/x-1-trace")
    assert env["AGENT_GRAPHS_TRACE_DIR"] == "/runs/x-1-trace"
    assert env["PATH"] == "/opt/coxswain-graphs/.venv/bin:/usr/bin"


def test_child_env_without_trace_dir_leaves_the_key_untouched():
    absent = route.child_env({"PATH": "/usr/bin"})
    assert "AGENT_GRAPHS_TRACE_DIR" not in absent
    present = route.child_env({"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/old"})
    assert present["AGENT_GRAPHS_TRACE_DIR"] == "/old"


def test_child_env_replaces_a_callers_pre_set_trace_dir():
    environ = {"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/old"}
    env = route.child_env(environ, trace_dir="/runs/x-1-trace")
    assert env["AGENT_GRAPHS_TRACE_DIR"] == "/runs/x-1-trace"


FIXTURE_PROFILE = {
    "team": "acme",
    "cartridges_dir": "/opt/cartridges",
    "skills_roots": ["/opt/skills-a", "/opt/skills-b"],
    "provider_profile": "/opt/providers/acme.yaml",
    "harness_dir": "/opt/coxswain-graphs",
    "workspace_dir": "/home/acme/workspace",
    "assume": "a",
}

FIXTURE_INTAKE = {
    "queued": [{"id": "ship-it", "title": "ship it"}],
    "decomposed": [{"id": "fix-ci", "title": "fix ci"}],
    "landed": [],
}

EMPTY_INTAKE_GROUPS = {"queued": [], "decomposed": [], "landed": []}

FIXTURE_RUNS = [
    {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
]

FIXTURE_INITIATIVES = [
    {"id": "widget", "phase": "build", "ready": 3},
]


def test_render_context_with_profile_matches_spec_layout():
    text = route.render_context(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS, FIXTURE_INITIATIVES
    )
    assert text == (
        "routing: team acme; work requests go through the route-work skill, "
        "questions stay inline\n"
        "intake: 1 queued, 1 decomposed, 0 landed\n"
        "lanes: 1 busy — widget-1 (pid 4242, since 14:02)\n"
        "ready: widget (3 tasks ready in phase build)"
    )


def test_render_context_with_profile_and_no_activity():
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [])
    assert text == (
        "routing: team acme; work requests go through the route-work skill, "
        "questions stay inline\n"
        "intake: 0 queued, 0 decomposed, 0 landed\n"
        "lanes: all clear\n"
        "ready: none"
    )


def test_render_context_appends_the_gate_line_when_given():
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [], gate_level="phase")
    assert text.splitlines()[-1] == "gate: phase"


def test_efficiency_line_states_spend_lands_cost_per_land_and_first_try_rate():
    assert route.efficiency_line(3.0, 4, 0.75) == "efficiency: today $3.00 for 4 landed ($0.75 per landed task), first-try 75%"


def test_efficiency_line_with_no_lands_says_nothing_landed_yet():
    assert route.efficiency_line(1.234, 0, 0.0) == "efficiency: today $1.23, nothing landed yet"


def test_efficiency_line_with_no_store_is_none():
    assert route.efficiency_line(None, 0, 0.0) is None


def test_efficiency_line_with_no_measured_task_says_first_try_unknown():
    assert route.efficiency_line(2.0, 1, None, 0) == "efficiency: today $2.00 for 1 landed ($2.00 per landed task), first-try unknown"


def test_efficiency_line_names_how_many_tasks_the_rate_covers_when_some_are_unmeasured():
    assert route.efficiency_line(3.0, 3, 0.5, 2) == "efficiency: today $3.00 for 3 landed ($1.00 per landed task), first-try 50% of 2 measured"


def test_render_context_puts_the_efficiency_line_after_lanes():
    line = "efficiency: today $0.00, nothing landed yet"
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [], efficiency=line)
    assert text.splitlines()[2:5] == ["lanes: all clear", line, "ready: none"]


def test_render_context_without_an_efficiency_line_is_unchanged():
    assert "efficiency" not in route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [])


LAND_LOG = "\n".join([
    '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": ["build", "mark_done"], "exit": 0}',
    '{"ts": "2026-09-25T02:00:00+00:00", "run": "r-1", "task": "t2", "steps_reached": ["build"], "exit": 0}',
    '{"ts": "2026-09-25T03:00:00+00:00", "run": "r-1", "task": "t3", "steps_reached": ["mark_done"], "exit": 1}',
    '{"ts": "2026-09-24T23:00:00+00:00", "run": "r-1", "task": "t4", "steps_reached": ["mark_done"], "exit": 0}',
    "not json",
    "[1, 2]",
    '{"ts": "2026-09-25T04:00:00+00:00", "run": "r-2", "task": null, "steps_reached": ["mark_done"], "exit": 0}',
])


def test_landed_today_keeps_only_clean_mark_done_lines_of_the_day_and_skips_garbage():
    assert route.landed_today(LAND_LOG, "2026-09-25") == [("r-1", "t1"), ("r-2", None)]


def test_landed_today_ignores_a_line_whose_steps_reached_is_a_string_not_a_list():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": "mark_done", "exit": 0}'
    assert route.landed_today(line, "2026-09-25") == []


def test_landed_today_counts_a_task_logged_twice_once():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": ["mark_done"], "exit": 0}'
    assert route.landed_today(f"{line}\n{line}", "2026-09-25") == [("r-1", "t1")]


def test_landed_today_counts_two_lines_with_no_task_twice():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": null, "steps_reached": ["mark_done"], "exit": 0}'
    assert route.landed_today(f"{line}\n{line}", "2026-09-25") == [("r-1", None), ("r-1", None)]


def test_a_composite_with_a_truncated_head_belongs_to_the_one_ticket_it_prefixes():
    assert route.task_owner("epic-x-5:seams:seams-t", "epic-x-5", {"seams-task", "wires-task"}) == "seams-task"


def test_a_bare_task_id_belongs_to_the_ticket_it_names():
    assert route.task_owner("seams-task", "epic-x-5", {"seams-task", "seams-task-two"}) == "seams-task"


def test_a_composite_from_another_run_belongs_to_no_ticket():
    assert route.task_owner("epic-x-4:seams:seams-t", "epic-x-5", {"seams-task"}) is None


def test_a_composite_whose_head_prefixes_two_landed_tickets_belongs_to_neither():
    assert route.task_owner("epic-x-5:seams:seams-task", "epic-x-5", {"seams-task", "seams-task-two"}) is None


def test_a_composite_whose_head_names_only_the_longer_ticket_belongs_to_it():
    assert route.task_owner("epic-x-5:seams:seams-task-two", "epic-x-5", {"seams-task", "seams-task-two"}) == "seams-task-two"


def test_first_try_is_the_share_of_measured_tasks_with_one_build_call_and_leaves_unmeasured_ones_out():
    landed = [("r-1", "alpha"), ("r-1", "beta"), ("r-1", "gamma"), ("r-2", None)]
    assert route.first_try(landed, {"r-1:build:alpha": 1, "r-1:build:bet": 2}) == (0.5, 2)


def test_first_try_never_gives_a_sibling_ticket_the_calls_of_its_shorter_named_neighbour():
    landed = [("r-1", "seams-task"), ("r-1", "seams-task-two")]
    assert route.first_try(landed, {"r-1:p:seams-task": 1, "r-1:p:seams-task-two": 2}) == (0.0, 1)


def test_first_try_with_nothing_measured_is_an_unknown_share():
    assert route.first_try([("r-1", "alpha")], {}) == (None, 0)


def test_render_context_without_profile_is_the_one_liner():
    text = route.render_context(None, [], [], [])
    assert text == (
        "routing: no profile at ~/.config/agent-tools/profile.yaml; the "
        "harness is not configured on this machine"
    )


def test_context_document_keys_on_exactly_the_profile_fields():
    doc = route.context_document(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS, FIXTURE_INITIATIVES
    )
    assert doc["team"] == "acme"
    assert doc["cartridges_dir"] == "/opt/cartridges"
    assert doc["skills_roots"] == ["/opt/skills-a", "/opt/skills-b"]
    assert doc["provider_profile"] == "/opt/providers/acme.yaml"
    assert doc["harness_dir"] == "/opt/coxswain-graphs"
    assert doc["workspace_dir"] == "/home/acme/workspace"
    assert doc["assume"] == "a"
    assert doc["intake"] == FIXTURE_INTAKE
    assert doc["runs"] == FIXTURE_RUNS
    assert doc["initiatives"] == FIXTURE_INITIATIVES
    assert json.loads(json.dumps(doc)) == doc


FIXTURE_RUNS_MIXED = [
    {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
    {"id": "widget-2", "pid": 4243, "alive": False, "started": "13:02"},
    {"id": "widget-3", "pid": 4244, "alive": False, "started": "12:02"},
]


def test_render_context_counts_only_alive_runs_in_flight():
    text = route.render_context(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS_MIXED, FIXTURE_INITIATIVES
    )
    lines = text.splitlines()
    assert "lanes: 1 busy — widget-1 (pid 4242, since 14:02)" in lines
    assert "widget-2" not in text
    assert "widget-3" not in text


def test_render_context_all_exited_reports_zero_in_flight():
    runs = [
        {"id": "widget-2", "pid": 4243, "alive": False, "started": "13:02"},
        {"id": "widget-3", "pid": 4244, "alive": False, "started": "12:02"},
    ]
    text = route.render_context(FIXTURE_PROFILE, FIXTURE_INTAKE, runs, FIXTURE_INITIATIVES)
    lines = text.splitlines()
    assert "lanes: all clear" in lines
    assert "widget-2" not in text
    assert "widget-3" not in text


def test_the_docket_lanes_line_for_none_and_for_two_busy_lanes():
    def lanes_line(runs):
        text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, runs, [])
        return next(l for l in text.splitlines() if l.startswith("lanes:"))

    two = [
        {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
        {"id": "widget-2", "pid": 4243, "alive": True, "started": "13:02"},
    ]
    assert lanes_line([]) == "lanes: all clear"
    assert lanes_line(two) == (
        "lanes: 2 busy — widget-1 (pid 4242, since 14:02), widget-2 (pid 4243, since 13:02)"
    )


def test_context_document_adds_a_live_count_of_alive_runs():
    doc = route.context_document(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS_MIXED, FIXTURE_INITIATIVES
    )
    assert doc["live"] == 1
    assert doc["runs"] == FIXTURE_RUNS_MIXED


def test_context_document_without_profile_has_the_same_keys_as_none():
    doc = route.context_document(None, [], [], [])
    for field in (
        "team",
        "cartridges_dir",
        "skills_roots",
        "provider_profile",
        "harness_dir",
        "workspace_dir",
        "assume",
    ):
        assert doc[field] is None
    assert doc["intake"] == []
    assert doc["runs"] == []
    assert doc["initiatives"] == []
    assert json.loads(json.dumps(doc)) == doc


def test_status_rows_covers_live_dead_and_no_pidfile_runs_in_order():
    entries = [
        {
            "id": "widget-1",
            "pid": 4242,
            "alive": True,
            "started": "14:02",
            "quarantined": [],
            "reused": [],
            "summary": None,
            "usage": None,
        },
        {
            "id": "widget-2",
            "pid": 4300,
            "alive": False,
            "started": "13:10",
            "quarantined": ["quarantined task foo"],
            "reused": [],
            "summary": "epic done",
            "usage": "usage $0.42",
        },
        {"id": "widget-3", "pid": None, "alive": None, "started": None},
    ]
    rows = route.status_rows(entries)
    assert [r["id"] for r in rows] == ["widget-1", "widget-2", "widget-3"]
    assert rows[0] == {
        "id": "widget-1",
        "pid": 4242,
        "state": "alive",
        "started": "14:02",
        "quarantined": [],
        "reused": [],
        "summary": None,
        "usage": None,
    }
    assert rows[1] == {
        "id": "widget-2",
        "pid": 4300,
        "state": "exited",
        "started": "13:10",
        "quarantined": ["quarantined task foo"],
        "reused": [],
        "summary": "epic done",
        "usage": "usage $0.42",
    }
    assert rows[2] == {
        "id": "widget-3",
        "pid": None,
        "state": "no pidfile",
        "started": None,
        "quarantined": [],
        "reused": [],
        "summary": None,
        "usage": None,
    }


def test_parse_frontmatter_returns_empty_fields_when_there_is_no_leading_fence():
    text = "Just a plain file.\nNo frontmatter here.\n"
    assert route.parse_frontmatter(text) == ({}, text)


def test_parse_frontmatter_returns_empty_fields_when_the_fence_never_closes():
    text = "---\ntitle: Fix the Bug\nNo closing fence follows.\n"
    assert route.parse_frontmatter(text) == ({}, text)


@pytest.mark.parametrize(
    "title",
    [
        "Fix the Bug",
        "route.py: initiative_files #wip",
        "  Fix the Bug  ",
        "[draft] ship it",
        '"draft" ship it',
    ],
)
def test_parse_frontmatter_round_trips_every_title_shape_initiative_files_can_emit(title):
    files = route.initiative_files(title, "Ship it.", "repo-url")
    task_text = next(v for k, v in files.items() if k.endswith(".md") and "/build/" in k)
    fields, _ = route.parse_frontmatter(task_text)
    assert fields["title"] == title
    assert fields["needs"] == []
    assert fields["surfaces"] == []


@pytest.mark.parametrize(
    "title",
    [
        "Fix the Bug",
        "route.py: initiative_files #wip",
        "  Fix the Bug  ",
        "[draft] ship it",
        '"draft" ship it',
    ],
)
def test_parse_frontmatter_round_trips_every_title_shape_intake_file_can_emit(title):
    files = route.intake_file(title, "Ship it.", "repo-url", "2026-09-04")
    text = next(iter(files.values()))
    fields, _ = route.parse_frontmatter(text)
    assert fields["title"] == title


def test_parse_frontmatter_splits_a_non_empty_list_on_commas():
    # The contract's list form is "flat list of comma-separated bare
    # items", not just the `[]` case every needs/surfaces assertion above
    # exercises. Returning `[inner]` in place of the comma split keeps
    # every other list assertion in this file green, so this is the one
    # test that pins the split and the per-item strip.
    text = "---\nneeds: [a, b]\n---\n\nx\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == ["a", "b"]


def test_parse_frontmatter_block_list_matches_flow_list():
    flow_text = "---\nneeds: [a, b]\nsurfaces: [c]\n---\n\nx\n"
    block_text = "---\nneeds:\n  - a\n  - b\nsurfaces:\n  - c\n---\n\nx\n"
    flow_fields, _ = route.parse_frontmatter(flow_text)
    block_fields, _ = route.parse_frontmatter(block_text)
    assert flow_fields == block_fields


def test_parse_frontmatter_block_list_does_not_swallow_the_next_key():
    text = "---\nneeds:\n  - a\ntitle: x\n---\n\nbody\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == ["a"]
    assert fields["title"] == "x"


def test_parse_frontmatter_empty_block_yields_empty_list():
    text = "---\nneeds:\ntitle: x\n---\n\nbody\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == []
    assert fields["title"] == "x"


def test_parse_frontmatter_round_trips_an_empty_string_field():
    # `title`'s own empty string is unreachable through these builders —
    # `_slug_or_raise` refuses it before any frontmatter is written — but
    # `repo` carries no such guard, so it is what exercises _yaml_scalar's
    # `value == ""` branch end to end here.
    files = route.initiative_files("Fix the Bug", "Ship it.", "")
    initiative_text = files["work/fix-the-bug/initiative.md"]
    fields, _ = route.parse_frontmatter(initiative_text)
    assert fields["repo"] == ""


def test_parse_frontmatter_recovers_the_body_after_the_closing_fence():
    # Exercises the blank-line strip and the trailing-newline strip on
    # their own: swapping either for a bare `after_closing` return still
    # passes every test above (they only check `fields`), so this is the
    # one assertion that would catch that regression.
    files = route.initiative_files("Fix the Bug", "Ship it.", "repo-url")
    task_text = files["work/fix-the-bug/build/fix-the-bug.md"]
    _, body = route.parse_frontmatter(task_text)
    assert body == "Ship it."


def test_parse_frontmatter_never_raises_on_a_header_line_with_no_colon_space():
    # A stray line inside the fence with no `": "` separator — a bad hand
    # edit, a YAML block-list item, a bare comment — is not a line this
    # module ever wrote. The contract says parse_frontmatter never raises
    # on any input, not just well-formed frontmatter, and the next phase's
    # list builders will read whatever is actually on disk.
    text = "---\nnotes\nid: x\n---\n\nbody\n"
    fields, body = route.parse_frontmatter(text)
    assert fields == {"id": "x"}
    assert body == "body"


def test_parse_frontmatter_misparses_a_bare_value_that_happens_to_start_with_a_quote():
    # Parser-side property, not a writer guard: this never calls
    # initiative_files or _yaml_scalar. It hand-builds the frontmatter line
    # the writer would emit for a `"`-leading title *if* it did not quote
    # it, to show why the writer must — parse_frontmatter has no way to
    # tell that unquoted value apart from a real quoted scalar, so it
    # slices the wrong substring back out. The actual guard against a
    # writer regression is the '"draft" ship it' case in the two
    # parametrised round-trip tests above: revert the _yaml_scalar
    # addition and the writer emits this same bare line, so that case's
    # `fields["title"] == title` assertion fails.
    bare_line = 'title: "draft" ship it\n'
    text = f"---\nid: x\n{bare_line}---\n\nShip it.\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["title"] != '"draft" ship it'


def test_intake_entries_sorts_by_filename_and_skips_consumed_prefix():
    # Filenames supplied out of order and one already-consumed file: the
    # sort and the skip are both live rules, not accidents of dict order.
    files = {
        "2026-09-03-later.md": "---\nid: later\ntitle: Later\n---\n\nBody.\n",
        "2026-09-01-earlier.md": "---\nid: earlier\ntitle: Earlier\n---\n\nBody.\n",
        "consumed/2026-08-01-old.md": "---\nid: old\ntitle: Old\n---\n\nBody.\n",
    }
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "earlier", "title": "Earlier", "initiative": None, "done": False, "path": "intake/2026-09-01-earlier.md"},
        {"id": "later", "title": "Later", "initiative": None, "done": False, "path": "intake/2026-09-03-later.md"},
    ]


def test_intake_entries_falls_back_to_stem_and_first_body_line_when_frontmatter_is_absent():
    files = {"2026-09-02-no-frontmatter.md": "\n\nFirst real line.\nSecond line.\n"}
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "2026-09-02-no-frontmatter", "title": "First real line.", "initiative": None, "done": False,
         "path": "intake/2026-09-02-no-frontmatter.md"}
    ]


def test_intake_entries_reads_the_initiative_field_and_the_done_prefix():
    files = {"done/2026-09-01-old.md": "---\nid: old\ntitle: Old\ninitiative: old\n---\n\nBody.\n"}
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "old", "title": "Old", "initiative": "old", "done": True, "path": "intake/2026-09-01-old.md"}
    ]


def test_run_entries_sorts_by_id_and_treats_a_non_integer_pidfile_as_dead():
    # Ids supplied out of order and one pidfile whose text is not an
    # integer — a partial write — must come back pid None, alive False even
    # though the caller's own `alive` map says True.
    pids = {"r2": "222", "r1": "not-a-pid"}
    alive = {"r1": True, "r2": True}
    started = {"r1": "2026-09-04T00:00:00", "r2": "2026-09-04T00:01:00"}
    entries = route.run_entries(pids, alive, started)
    assert entries == [
        {"id": "r1", "pid": None, "alive": False, "started": "2026-09-04T00:00:00", "heartbeat": None,
         "host": None, "remote": False},
        {"id": "r2", "pid": 222, "alive": True, "started": "2026-09-04T00:01:00", "heartbeat": None,
         "host": None, "remote": False},
    ]


def test_run_entries_carries_a_heartbeat_only_for_the_runs_the_caller_names():
    entries = route.run_entries({"r1": "1", "r2": "2"}, {}, {}, {"r2": "2026-09-25T10:38:12Z"})
    assert [e["heartbeat"] for e in entries] == [None, "2026-09-25T10:38:12Z"]


_LEASED_RUN = {"id": "widget-1", "pid": 4242, "alive": True, "started": "2026-09-25T10:38:00+00:00",
               "heartbeat": "2026-09-25T10:40:00Z"}


@pytest.fixture
def utc_plus_2(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _lanes(run, now):
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [run], [], now=now)
    return next(l for l in text.splitlines() if l.startswith("lanes:"))


def test_a_lane_with_a_heartbeat_reads_since_and_seconds_ago(utc_plus_2):
    assert _lanes(_LEASED_RUN, "2026-09-25T10:40:12Z") == "lanes: 1 busy — widget-1 (since 12:38, heartbeat 12s ago)"


def test_a_lane_with_a_heartbeat_reads_minutes_ago_from_sixty_seconds(utc_plus_2):
    assert _lanes(_LEASED_RUN, "2026-09-25T10:43:00Z").endswith("heartbeat 3m ago)")
    assert _lanes(_LEASED_RUN, "2026-09-25T10:40:59Z").endswith("heartbeat 59s ago)")
    assert _lanes(_LEASED_RUN, "2026-09-25T10:41:00Z").endswith("heartbeat 1m ago)")


def test_a_lane_without_a_heartbeat_keeps_the_pid_form_with_local_since(utc_plus_2):
    run = {**_LEASED_RUN, "heartbeat": None}
    assert _lanes(run, "2026-09-25T10:43:00Z") == "lanes: 1 busy — widget-1 (pid 4242, since 12:38)"


class _FakeLane(NamedTuple):
    run: str
    host: str | None
    launched_at: str
    heartbeat_at: str


_REMOTE_LANE = _FakeLane("widget-2", "host-b", "2026-09-25T10:38:00+00:00", "2026-09-25T10:40:00Z")


def test_a_remote_lane_becomes_an_entry_with_its_host():
    assert route.remote_lane_entries([_REMOTE_LANE]) == [
        {"id": "widget-2", "pid": None, "alive": True, "started": "2026-09-25T10:38:00+00:00",
         "heartbeat": "2026-09-25T10:40:00Z", "host": "host-b", "remote": True}
    ]


def test_a_remote_lane_with_no_host_keeps_host_none_and_remote_true():
    entry = route.remote_lane_entries([_REMOTE_LANE._replace(host=None)])[0]
    assert (entry["host"], entry["remote"]) == (None, True)


def test_a_local_entry_reads_remote_false_and_host_none():
    entry = route.run_entries({"r1": "7"}, {"r1": True}, {"r1": "2026-09-25T10:38:00+00:00"})[0]
    assert (entry["host"], entry["remote"]) == (None, False)


def test_local_entries_come_first_then_remote_entries_in_input_order():
    local = route.run_entries({"b": "1", "a": "2"}, {}, {})
    remote = route.remote_lane_entries([_REMOTE_LANE._replace(run="z"), _REMOTE_LANE._replace(run="y")])
    assert [e["id"] for e in local + remote] == ["a", "b", "z", "y"]
    assert [e["remote"] for e in local + remote] == [False, False, True, True]


def test_a_remote_lane_line_names_its_host(utc_plus_2):
    entry = route.remote_lane_entries([_REMOTE_LANE])[0]
    assert _lanes(entry, "2026-09-25T10:40:12Z") == "lanes: 1 busy — widget-2 (on host-b, since 12:38, heartbeat 12s ago)"


def test_a_remote_lane_line_without_a_host_says_another_machine(utc_plus_2):
    entry = route.remote_lane_entries([_REMOTE_LANE._replace(host=None)])[0]
    assert _lanes(entry, "2026-09-25T10:43:00Z") == (
        "lanes: 1 busy — widget-2 (on another machine, since 12:38, heartbeat 3m ago)"
    )


def test_initiative_summaries_picks_the_sorted_first_phase_with_a_ready_task():
    # Two phases both hold a ready, unblocked task; the alphabetically
    # first phase name must win, not first-in-list or last-in-list.
    items = [
        {"id": "a1", "initiative": "alpha", "phase": "zzz-later", "state": "ready", "needs": []},
        {"id": "a2", "initiative": "alpha", "phase": "aaa-first", "state": "ready", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "alpha", "phase": "aaa-first", "ready": 1}]


def test_initiative_summaries_excludes_a_task_whose_needs_are_not_all_done():
    # The one ready task in this initiative needs a task that is not done,
    # so it must not be counted, and the initiative must not appear at all.
    items = [
        {"id": "b1", "initiative": "beta", "phase": "build", "state": "ready", "needs": ["b0"]},
        {"id": "b0", "initiative": "beta", "phase": "build", "state": "in_progress", "needs": []},
    ]
    assert route.initiative_summaries(items) == []


def test_initiative_summaries_ready_is_a_count_not_a_flag():
    # Two ready, unblocked tasks share the winning phase and a third ready
    # task sits in a losing phase; a literal 1 in place of a real count
    # would pass every other case in this file but fails here.
    items = [
        {"id": "g1", "initiative": "gamma", "phase": "build", "state": "ready", "needs": []},
        {"id": "g2", "initiative": "gamma", "phase": "build", "state": "ready", "needs": []},
        {"id": "g3", "initiative": "gamma", "phase": "later", "state": "ready", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "gamma", "phase": "build", "ready": 2}]


def test_initiative_summaries_a_dropped_item_satisfies_a_dependents_needs_and_is_never_itself_ready():
    # d0 is dropped, terminal but never "ready"; d2 needs both d0 and d1
    # (done) and must come back ready — the dropped item unblocks it without
    # ever counting as a ready task itself.
    items = [
        {"id": "d0", "initiative": "delta", "phase": "build", "state": "dropped", "needs": []},
        {"id": "d1", "initiative": "delta", "phase": "build", "state": "done", "needs": []},
        {"id": "d2", "initiative": "delta", "phase": "build", "state": "ready", "needs": ["d0", "d1"]},
    ]
    assert route.initiative_summaries(items) == [{"id": "delta", "phase": "build", "ready": 1}]


_HELD = {"id": "cli", "initiative": "sync", "phase": "2-edge", "state": "blocked", "needs": [], "file": "2-edge/cli.md"}
_HOOKS = {"id": "hooks", "initiative": "sync", "phase": "3-hooks", "state": "ready", "needs": [], "file": "3-hooks/hooks.md"}


def test_initiative_summaries_omits_an_initiative_whose_only_ready_task_sits_behind_a_blocked_phase():
    assert route.initiative_summaries([_HELD, _HOOKS]) == []


def test_initiative_summaries_omits_a_ready_task_that_needs_a_blocked_item():
    same_phase = {**_HOOKS, "phase": "2-edge", "needs": ["cli"]}
    assert route.initiative_summaries([_HELD, same_phase]) == []


def test_initiative_summaries_keeps_an_initiative_with_one_ready_task_not_behind_a_blocked_item():
    early = {"id": "e", "initiative": "sync", "phase": "1-first", "state": "ready", "needs": [], "file": "1-first/e.md"}
    assert route.initiative_summaries([_HELD, _HOOKS, early]) == [{"id": "sync", "phase": "1-first", "ready": 1}]


def test_launch_blockers_names_the_blocked_file_a_ready_task_sits_behind():
    assert route.launch_blockers([_HELD, _HOOKS]) == ["2-edge/cli.md"]


def test_launch_blockers_is_empty_with_no_blocked_item_or_no_ready_task():
    assert route.launch_blockers([_HOOKS]) == []
    assert route.launch_blockers([_HELD]) == []


def test_launch_blockers_ignores_a_blocked_item_in_a_later_phase():
    late = {**_HELD, "phase": "4-late", "file": "4-late/cli.md"}
    assert route.launch_blockers([late, _HOOKS]) == []


def test_initiative_summaries_sorts_rows_by_initiative_id():
    # Six initiatives, each with one ready, unblocked task, supplied in
    # reverse order. Grouping them with a bare set (no `sorted`) would come
    # back in whatever order Python's per-process hash randomisation gives
    # a six-element string set, which matches this exact ascending order by
    # chance on 1 run in 720 — this is a real pin, not a coin flip.
    items = [
        {"id": f"{name[0]}1", "initiative": name, "phase": "build", "state": "ready", "needs": []}
        for name in ["zulu", "yankee", "xray", "whiskey", "victor", "alpha"]
    ]
    summaries = route.initiative_summaries(items)
    assert [row["id"] for row in summaries] == [
        "alpha", "victor", "whiskey", "xray", "yankee", "zulu",
    ]


def test_state_problems_names_the_initiative_file_and_bad_value():
    items = [
        {"initiative": "alpha", "file": "1-build/t1.md", "state": "superseded"},
        {"initiative": "alpha", "file": "1-build/t2.md", "state": "ready"},
    ]
    assert route.state_problems(items) == [
        "alpha: 1-build/t1.md: unknown state 'superseded'",
    ]


def test_initiative_summaries_an_unknown_state_makes_the_initiative_unlaunchable():
    # e1 is otherwise ready and unblocked, but e0's bad state must zero out
    # the whole initiative, not just skip e0.
    items = [
        {"id": "e0", "initiative": "epsilon", "phase": "build", "state": "superseded",
         "needs": [], "file": "build/e0.md"},
        {"id": "e1", "initiative": "epsilon", "phase": "build", "state": "ready", "needs": []},
    ]
    assert route.initiative_summaries(items) == [{"id": "epsilon", "phase": None, "ready": 0}]


def test_initiative_summaries_an_approved_item_is_awaiting_merge_not_ready():
    items = [
        {"id": "f0", "initiative": "foxtrot", "phase": "build", "state": "approved", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "foxtrot", "phase": None, "ready": 0, "awaiting_merge": 1}]


def test_initiative_states_an_approved_item_is_not_complete():
    items = [{"initiative": "foxtrot", "state": "approved"}]
    assert route.initiative_states(["foxtrot"], items) == {"foxtrot": False}


def test_intake_entries_falls_back_to_id_when_frontmatter_has_no_title_and_body_is_empty():
    # Frontmatter with an id but no title, and no body line at all: title
    # must fall back to id, not raise on an empty body_lines list.
    files = {"any-name.md": "---\nid: notitle\n---\n\n"}
    entries = route.intake_entries(files)
    assert entries == [{"id": "notitle", "title": "notitle", "initiative": None, "done": False, "path": "intake/any-name.md"}]


def test_parse_pid_reads_a_plain_digit_string():
    assert route.parse_pid("4821") == 4821


def test_parse_pid_returns_none_for_an_empty_string():
    assert route.parse_pid("") is None
    assert route.parse_pid("   ") is None


def test_parse_pid_returns_none_for_garbage():
    assert route.parse_pid("garbage") is None


def test_render_status_no_pidfile_row_omits_pid_and_started():
    rows = [{"id": "r1", "pid": None, "state": "no pidfile", "started": None,
             "quarantined": [], "reused": [], "summary": None, "usage": None}]
    assert route.render_status(rows) == "r1: no pidfile"


def test_render_status_quiet_row_with_a_pid_stays_one_line():
    rows = [{"id": "r2", "pid": 123, "state": "alive", "started": "2026-09-04T00:00:00+00:00",
             "quarantined": [], "reused": [], "summary": None, "usage": None}]
    assert route.render_status(rows) == "r2: alive (pid 123, started 2026-09-04T00:00:00+00:00)"


def test_render_status_appends_quarantined_reused_summary_and_usage():
    rows = [{"id": "r3", "pid": None, "state": "no pidfile", "started": None,
             "quarantined": ["quarantined task: a — reason"], "reused": ["reused b from run-1"],
             "summary": "epic run-2: done", "usage": "usage: 1 call"}]
    assert route.render_status(rows) == (
        "r3: no pidfile quarantined task: a — reason reused b from run-1 epic run-2: done usage: 1 call"
    )


def test_render_status_appends_intake_groups_with_names_when_given():
    groups = {
        "queued": [{"id": "q1", "title": "Q"}],
        "decomposed": [{"id": "d1", "title": "D"}],
        "landed": [],
    }
    assert route.render_status([], groups) == (
        "intake queued: 1 — q1\nintake decomposed: 1 — d1\nintake landed: 0"
    )


def test_render_status_omits_intake_section_when_groups_is_none():
    rows = [{"id": "a", "pid": None, "state": "no pidfile", "started": None,
             "quarantined": [], "reused": [], "summary": None, "usage": None}]
    assert route.render_status(rows) == "a: no pidfile"


def test_render_status_appends_the_gate_line_when_given():
    rows = [{"id": "a", "pid": None, "state": "no pidfile", "started": None,
             "quarantined": [], "reused": [], "summary": None, "usage": None}]
    assert route.render_status(rows, gate_level="ticket") == "a: no pidfile\ngate: ticket"


def test_link_intake_adds_both_fields_and_keeps_everything_else():
    initiative_text = "---\nid: foo\ntitle: Foo\nrepo: acme/foo\n---\n\nDo the foo thing.\n"
    intake_text = "---\nid: foo\ntitle: Foo\nrepo: acme/foo\n---\n\nDo the foo thing.\n"
    new_initiative, new_intake = route.link_intake(
        initiative_text, intake_text, "intake/2026-09-03-foo.md", "foo"
    )
    initiative_fields, initiative_body = route.parse_frontmatter(new_initiative)
    intake_fields, intake_body = route.parse_frontmatter(new_intake)
    assert initiative_fields == {"id": "foo", "title": "Foo", "repo": "acme/foo", "intake": "intake/2026-09-03-foo.md"}
    assert intake_fields == {"id": "foo", "title": "Foo", "repo": "acme/foo", "initiative": "foo"}
    assert initiative_body == "Do the foo thing."
    assert intake_body == "Do the foo thing."


def test_link_intake_updates_an_existing_field_in_place_instead_of_duplicating_it():
    initiative_text = "---\nid: foo\nintake: intake/old.md\n---\n\nBody.\n"
    intake_text = "---\nid: foo\ninitiative: foo\n---\n\nBody.\n"
    new_initiative, new_intake = route.link_intake(initiative_text, intake_text, "intake/new.md", "foo")
    assert new_initiative.count("intake:") == 1
    assert new_intake.count("initiative:") == 1
    assert route.parse_frontmatter(new_initiative)[0]["intake"] == "intake/new.md"


def test_initiative_states_is_false_for_an_id_with_no_items_and_for_one_not_all_done():
    items = [
        {"initiative": "alpha", "state": "done"},
        {"initiative": "beta", "state": "ready"},
    ]
    assert route.initiative_states(["alpha", "beta", "gamma"], items) == {
        "alpha": True, "beta": False, "gamma": False,
    }


def test_initiative_states_an_initiative_with_zero_items_is_not_done():
    assert route.initiative_states(["z"], []) == {"z": False}


def test_initiative_states_an_initiative_with_all_items_done_is_done():
    assert route.initiative_states(["a"], [{"initiative": "a", "state": "done"}]) == {"a": True}


def test_initiative_states_reads_done_plus_dropped_as_complete():
    items = [
        {"initiative": "alpha", "state": "done"},
        {"initiative": "alpha", "state": "dropped"},
    ]
    assert route.initiative_states(["alpha"], items) == {"alpha": True}


def test_initiative_states_done_plus_ready_reads_incomplete_with_the_ready_item_listed():
    # Guards against TERMINAL wrongly swallowing "ready": the initiative
    # must stay incomplete, and its one ready task must still show up.
    items = [
        {"id": "a1", "initiative": "alpha", "phase": "build", "state": "done", "needs": []},
        {"id": "a2", "initiative": "alpha", "phase": "build", "state": "ready", "needs": []},
    ]
    assert route.initiative_states(["alpha"], items) == {"alpha": False}
    assert route.initiative_summaries(items) == [{"id": "alpha", "phase": "build", "ready": 1}]


def test_intake_groups_classifies_a_done_plus_dropped_initiative_as_landed():
    # The "done" boolean here comes from initiative_states on raw item
    # states, not a hand-picked literal, so this proves the classification
    # path agrees with initiative_states about a done+dropped initiative.
    items = [
        {"initiative": "gamma", "state": "done"},
        {"initiative": "gamma", "state": "dropped"},
    ]
    done = route.initiative_states(["gamma"], items)["gamma"]
    initiatives = [{"id": "gamma", "done": done, "text": ""}]
    entry = {"id": "g1", "title": "G", "initiative": "gamma", "done": False, "path": "intake/g1.md"}
    groups = route.intake_groups([entry], initiatives)
    assert groups["landed"] == [entry]


def test_intake_groups_calls_an_intake_whose_initiative_has_zero_items_decomposed_not_landed():
    done = route.initiative_states(["x"], [])["x"]
    initiatives = [{"id": "x", "done": done, "text": ""}]
    entry = {"id": "x1", "title": "X", "initiative": "x", "done": False, "path": "intake/x1.md"}
    groups = route.intake_groups([entry], initiatives)
    assert groups["decomposed"] == [entry]
    assert groups["landed"] == []


def test_intake_groups_splits_queued_decomposed_landed_and_the_legacy_case():
    intake = [
        {"id": "q1", "title": "Q", "initiative": None, "done": False, "path": "intake/q1.md"},
        {"id": "d1", "title": "D", "initiative": "alpha", "done": False, "path": "intake/d1.md"},
        {"id": "l1", "title": "L", "initiative": "beta", "done": False, "path": "intake/l1.md"},
        {"id": "legacy-cited", "title": "Legacy cited", "initiative": None, "done": True,
         "path": "intake/2026-08-01-legacy-cited.md"},
        {"id": "legacy-uncited", "title": "Legacy uncited", "initiative": None, "done": True,
         "path": "intake/2026-08-02-legacy-uncited.md"},
        # id "ci" is a substring of "decided" in delta's text below, but delta
        # never cites this row's path: an id-substring match would wrongly
        # call this decomposed, where a path match correctly calls it landed.
        {"id": "ci", "title": "Ci", "initiative": None, "done": True, "path": "intake/2026-08-03-ci-notes.md"},
    ]
    initiatives = [
        {"id": "alpha", "done": False, "text": ""},
        {"id": "beta", "done": True, "text": ""},
        {"id": "gamma", "done": False, "text": "See intake/2026-08-01-legacy-cited.md for background."},
        {"id": "delta", "done": False, "text": "The team decided this independently."},
    ]
    groups = route.intake_groups(intake, initiatives)
    assert [e["id"] for e in groups["queued"]] == ["q1"]
    assert [e["id"] for e in groups["decomposed"]] == ["d1", "legacy-cited"]
    assert [e["id"] for e in groups["landed"]] == ["l1", "legacy-uncited", "ci"]


_INTAKE_NAMED = "---\nid: a\nintake: intake/a.md\n---\nbody\n"


def test_intake_groups_calls_an_intake_named_by_an_open_initiative_decomposed():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    groups = route.intake_groups([entry], [{"id": "x", "done": False, "text": _INTAKE_NAMED}])
    assert groups["decomposed"] == [entry]


def test_intake_groups_calls_an_intake_named_by_a_done_initiative_landed():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    groups = route.intake_groups([entry], [{"id": "x", "done": True, "text": _INTAKE_NAMED}])
    assert groups["landed"] == [entry]


def test_intake_groups_keeps_an_intake_named_by_no_initiative_queued():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    groups = route.intake_groups([entry], [{"id": "x", "done": False, "text": "---\nintake: intake/b.md\n---\n"}])
    assert groups["queued"] == [entry]


def test_render_status_joins_multiple_rows_with_one_newline_each():
    rows = [
        {"id": "a", "pid": None, "state": "no pidfile", "started": None,
         "quarantined": [], "reused": [], "summary": None, "usage": None},
        {"id": "b", "pid": 7, "state": "exited", "started": "t",
         "quarantined": [], "reused": [], "summary": None, "usage": None},
    ]
    assert route.render_status(rows) == "a: no pidfile\nb: exited (pid 7, started t)"


_NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


def _row(run_id: str, state: str, started: str | None) -> dict:
    return {"id": run_id, "state": state, "started": started}


def test_recent_rows_hides_and_counts_an_old_dead_row():
    rows = [_row("old", "exited", "2026-09-24T11:59:00+00:00")]
    assert route.recent_rows(rows, _NOW) == ([], 1)


def test_recent_rows_keeps_a_live_row_however_old():
    rows = [_row("live", "alive", "2026-08-01T00:00:00+00:00")]
    assert route.recent_rows(rows, _NOW) == (rows, 0)


def test_recent_rows_keeps_a_recent_dead_row():
    rows = [_row("new", "exited", "2026-09-25T00:00:00+00:00")]
    assert route.recent_rows(rows, _NOW) == (rows, 0)


def test_recent_rows_counts_a_dead_row_with_no_started_as_old():
    rows = [_row("none", "no pidfile", None), _row("bad", "exited", "garbage")]
    assert route.recent_rows(rows, _NOW) == ([], 2)


def test_status_entries_keeps_a_pid_bearing_run_untouched_when_it_has_no_log():
    runs = [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}]
    assert route.status_entries(runs, {}) == [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}]


def test_status_entries_merges_a_pid_bearing_run_with_its_log_summary():
    runs = [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}]
    summaries = {"run1": {"quarantined": ["quarantined task: a"], "reused": [], "summary": "epic run-1", "usage": None}}
    assert route.status_entries(runs, summaries) == [
        {"id": "run1", "pid": 123, "alive": True, "started": "t1",
         "quarantined": ["quarantined task: a"], "reused": [], "summary": "epic run-1", "usage": None}
    ]


def test_status_entries_gives_a_log_only_id_the_pid_less_shape():
    summaries = {"run2": {"quarantined": [], "reused": [], "summary": "epic run-2", "usage": None}}
    assert route.status_entries([], summaries) == [
        {"id": "run2", "pid": None, "alive": False, "started": None,
         "quarantined": [], "reused": [], "summary": "epic run-2", "usage": None}
    ]


def test_status_entries_sorts_the_union_of_run_and_log_ids():
    runs = [{"id": "b", "pid": 1, "alive": True, "started": "t"}]
    summaries = {"a": {}, "c": {}}
    ids = [entry["id"] for entry in route.status_entries(runs, summaries)]
    assert ids == ["a", "b", "c"]


def test_work_item_passes_full_frontmatter_through():
    fields = {"id": "t1", "phase": "1-build", "state": "ready", "needs": ["t0"]}
    item = route.work_item(fields, initiative="demo", phase_dir="9-ignored", stem="ignored")
    assert item == {
        "id": "t1", "initiative": "demo", "phase": "1-build", "state": "ready", "needs": ["t0"],
        "file": "9-ignored/ignored.md",
    }


def test_work_item_defaults_every_field_from_the_path_when_frontmatter_is_empty():
    item = route.work_item({}, initiative="demo", phase_dir="1-build", stem="task")
    assert item == {
        "id": "task", "initiative": "demo", "phase": "1-build", "state": "todo", "needs": [],
        "file": "1-build/task.md",
    }


def test_work_item_initiative_argument_wins_over_a_frontmatter_key():
    fields = {"initiative": "wrong"}
    item = route.work_item(fields, initiative="demo", phase_dir="1-build", stem="task")
    assert item["initiative"] == "demo"


def _assessment(verdict: str, reason: str) -> pacing.Assessment:
    return pacing.Assessment(
        spent_fraction=0.5, elapsed_fraction=0.5, projected_total=1.0, headroom_usd=1.0,
        verdict=verdict, tier_ceiling="deep", effort_ceiling="high", hold_until=None, reason=reason,
    )


def test_launch_gate_stop_without_force_refuses_with_exit_2_and_the_reason():
    code, lines = route.launch_gate(_assessment("stop", "both ladders exhausted"), force=False)
    assert code == 2
    assert lines == ["routing: usage stop: both ladders exhausted"]


def test_launch_gate_stop_with_force_overrides_and_continues():
    code, lines = route.launch_gate(_assessment("stop", "both ladders exhausted"), force=True)
    assert code is None
    assert lines == ["routing: usage stop overridden by --force: both ladders exhausted"]


def test_launch_gate_hold_narrates_the_reason_and_continues():
    code, lines = route.launch_gate(_assessment("hold", "headroom below minimum"), force=False)
    assert code is None
    assert lines == ["routing: usage hold: headroom below minimum"]


def test_launch_gate_go_degraded_is_an_advisory_and_continues():
    code, lines = route.launch_gate(_assessment("go_degraded", "ceilings tier=cheap effort=low"), force=False)
    assert code is None
    assert lines == ["routing: advisory: usage go_degraded: ceilings tier=cheap effort=low"]


def test_launch_gate_go_is_silent():
    code, lines = route.launch_gate(_assessment("go", "on pace"), force=False)
    assert code is None
    assert lines == []


def test_lint_items_flags_a_reach_violation():
    # work-shape.md §8: "a `~/` path" is the fixture named for this rule.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": "see ~/notes/plan.md for context"}]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem(
            "t1", "reach", "'~/notes/plan.md' is outside 'acme/widgets'",
            "move the artifact into the repository or drop the reference",
        )
    ]


def test_lint_items_does_not_flag_an_absolute_path_inside_the_repo():
    # work-shape.md §3: reach excludes a path "that is not inside the target repository".
    items = [{"task": "t1", "phase": "build", "surfaces": [],
              "body": "see /home/acme/widgets/agent_tools/route.py for the schema"}]
    problems = route.lint_items(items, "/home/acme/widgets", ())
    assert problems == []


def test_lint_items_flags_a_reach_violation_inside_a_backtick_span():
    # 2026-09-09 revise: this project's own house style wraps every file
    # reference in backticks, so the path sits right after one, not a space.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": "see `/tmp/notes.md` for context"}]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem(
            "t1", "reach", "'/tmp/notes.md' is outside 'acme/widgets'",
            "move the artifact into the repository or drop the reference",
        )
    ]


def test_lint_items_treats_a_url_route_as_notation_not_reach():
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": "POST `/v1/chat/completions` on the local server"}]
    assert [p for p in route.lint_items(items, "acme/widgets", ()) if p.rule == "reach"] == []


def test_lint_items_stands_the_reach_rule_down_when_repo_is_unresolved():
    # 2026-09-09 revise: no repo to check against means no reach findings,
    # not a reach finding on every path.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": "see ~/notes/plan.md for context"}]
    assert route.lint_items(items, None, ()) == []


def test_lint_items_flags_a_grant_violation():
    # work-shape.md §8: "a `cox` command" is the fixture named for this rule.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": "evidence: run `cox route status`"}]
    problems = route.lint_items(items, "acme/widgets", ("pytest", "git status", "git diff"))
    assert problems == [
        route.Problem(
            "t1", "grant", "command 'cox' is not in this role's grant",
            "name only pytest, git status, git diff",
        )
    ]


def test_lint_items_flags_a_coupling_violation():
    # work-shape.md §8: "two tickets sharing a test file" is the fixture named for this rule.
    items = [
        {"task": "t1", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "first ticket"},
        {"task": "t2", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "second ticket"},
    ]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem(
            "t1", "coupling", "t1 and t2 both surface 'tests/test_route.py'",
            "merge them, or move one to a later phase",
        )
    ]


def test_lint_items_flags_coupling_even_when_a_needs_edge_orders_the_pair():
    # same-phase-tickets-that-share-a-file-are-merged-wire-merge-into-lint-and-launch:
    # the needs exemption is gone, since merge_same_phase now folds a mergeable
    # pair before this check ever sees it; a needs edge no longer excuses one.
    items = [
        {"task": "t1", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "first ticket"},
        {"task": "t2", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "second ticket",
         "needs": ["t1"]},
    ]
    problems = route.lint_items(items, "acme/widgets", ())
    # lint-refuses-a-needs-edge-inside-one-phase: this same-phase needs edge
    # is now also a phase_needs deadlock in its own right.
    assert [p.rule for p in problems] == ["coupling", "phase_needs"]


def test_lint_items_flags_coupling_across_a_former_needs_chain():
    # A needs B, B needs C: previously exempted through transitive reach.
    items = [
        {"task": "a", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "a", "needs": ["b"]},
        {"task": "b", "phase": "build", "surfaces": [], "body": "b", "needs": ["c"]},
        {"task": "c", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "c"},
    ]
    problems = route.lint_items(items, "acme/widgets", ())
    # lint-refuses-a-needs-edge-inside-one-phase: both same-phase needs edges
    # in this chain (a needs b, b needs c) are also phase_needs deadlocks.
    assert [p.rule for p in problems] == ["coupling", "phase_needs", "phase_needs"]


def test_lint_items_pins_coupling_as_a_phase_scoped_rule_when_no_needs_edge_exists():
    # Cross-phase sharing is governed by phase order today, not by needs
    # reachability; this pins that the closure check changes nothing here.
    items = [
        {"task": "t1", "phase": "build", "surfaces": ["tests/test_route.py"], "body": "first ticket"},
        {"task": "t2", "phase": "ship", "surfaces": ["tests/test_route.py"], "body": "second ticket"},
    ]
    assert route.lint_items(items, "acme/widgets", ()) == []


def test_lint_items_flags_a_needs_edge_inside_one_phase():
    # lint-refuses-a-needs-edge-inside-one-phase: a needs edge between two
    # tickets sharing a phase always deadlocks a phase land.
    items = [
        {"task": "t1", "phase": "build", "surfaces": [], "body": "first ticket"},
        {"task": "t2", "phase": "build", "surfaces": [], "body": "second ticket",
         "needs": ["t1"]},
    ]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem(
            "t2", "phase_needs", "t2 needs t1 in the same phase 'build'",
            "move it to a later phase that needs this one",
        )
    ]


def test_lint_items_allows_a_needs_edge_across_phases():
    # A needs edge that already orders two phases is not a deadlock: the
    # needed ticket's phase lands before the dependent's phase starts.
    items = [
        {"task": "t1", "phase": "build", "surfaces": [], "body": "first ticket"},
        {"task": "t2", "phase": "ship", "surfaces": [], "body": "second ticket",
         "needs": ["t1"]},
    ]
    problems = route.lint_items(items, "acme/widgets", ())
    assert [p.rule for p in problems if p.rule == "phase_needs"] == []


def test_coupling_problems_flags_an_approved_ticket_sharing_a_surface_with_a_ready_one():
    # merge_same_phase never touches an `approved` ticket, so a same-phase
    # collision with it is a real problem, not one already folded away.
    items = [
        {"task": "t1", "phase": "build", "state": "approved", "surfaces": ["agent_tools/route.py"]},
        {"task": "t2", "phase": "build", "state": "ready", "surfaces": ["agent_tools/route.py"]},
    ]
    problems = route._coupling_problems(items)
    assert [p.rule for p in problems] == ["coupling"]


def test_coupling_problems_flags_a_needs_joined_ready_pair_sharing_a_surface():
    # The old needs exemption is gone: a needs edge no longer excuses a pair.
    items = [
        {"task": "t1", "phase": "build", "state": "ready", "surfaces": ["agent_tools/route.py"]},
        {"task": "t2", "phase": "build", "state": "ready", "surfaces": ["agent_tools/route.py"],
         "needs": ["t1"]},
    ]
    problems = route._coupling_problems(items)
    assert [p.rule for p in problems] == ["coupling"]


def test_coupling_problems_flags_a_shared_surface_that_is_not_a_test_file():
    # Broadened past test files: any shared surfaces entry counts.
    items = [
        {"task": "t1", "phase": "build", "surfaces": ["agent_tools/route.py"]},
        {"task": "t2", "phase": "build", "surfaces": ["agent_tools/route.py"]},
    ]
    problems = route._coupling_problems(items)
    assert problems == [
        route.Problem(
            "t1", "coupling", "t1 and t2 both surface 'agent_tools/route.py'",
            "merge them, or move one to a later phase",
        )
    ]


def test_merge_report_lines_names_members_merged_id_and_shared_surface():
    merges = [{"members": ["a", "b"], "into": "a", "surfaces": ["agent_tools/x.py"]}]
    assert route.merge_report_lines(merges) == [
        "merged: a + b -> a (shared agent_tools/x.py)"
    ]


# The shape `initiative_files` files with `budget_usd`, plus one attempt as the chair records it.
_FILED_TICKET = (
    "---\nid: a\nphase: build\nstate: ready\nneeds: []\nsurfaces: [agent_tools/x.py]\ntitle: Add A\n"
    "budget_usd: 2.0\nattempts:\n  - run: demo-1\n    ts: '2026-09-01'\n    body_sha: abc\nlint: []\n"
    "---\n\nBody A\n"
)


def test_merged_ticket_text_keeps_budget_attempts_and_lint_on_a_survivor():
    survivor = {
        "id": "a", "title": "Add A; Add B", "state": "ready", "phase": "build",
        "surfaces": ["agent_tools/x.py", "agent_tools/y.py"], "needs": ["c"],
        "body": "## Add A\nBody A\n\n## Add B\nBody B",
    }
    assert route.merged_ticket_text(_FILED_TICKET, survivor) == (
        "---\nid: a\nphase: build\nstate: ready\nneeds: [c]\nsurfaces: [agent_tools/x.py, agent_tools/y.py]\n"
        "title: Add A; Add B\nbudget_usd: 2.0\nattempts:\n  - run: demo-1\n    ts: '2026-09-01'\n"
        "    body_sha: abc\nlint: []\n---\n\n## Add A\nBody A\n\n## Add B\nBody B\n"
    )


def test_merged_ticket_text_marks_a_dropped_member_and_keeps_everything_else():
    dropped = {"id": "a", "title": "Add A", "state": "dropped", "merged_into": "z"}
    assert route.merged_ticket_text(_FILED_TICKET, dropped) == _FILED_TICKET.replace(
        "state: ready", "state: dropped"
    ).replace("lint: []\n---", "lint: []\nmerged_into: z\n---")


def test_merged_ticket_text_replaces_a_block_list_field_whole():
    original = "---\nid: a\nsurfaces:\n  - agent_tools/x.py\ntitle: A\n---\n\nBody\n"
    survivor = {"id": "a", "title": "A; B", "surfaces": ["agent_tools/x.py", "agent_tools/y.py"], "needs": [],
                "body": "Merged"}
    assert route.merged_ticket_text(original, survivor) == (
        "---\nid: a\nsurfaces: [agent_tools/x.py, agent_tools/y.py]\ntitle: A; B\nneeds: []\n---\n\nMerged\n"
    )


def test_lint_items_flags_a_size_violation():
    # work-shape.md §8: "a 750-word body" is the fixture this rule's test carries.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": " ".join(["word"] * 750)}]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem("t1", "size", "body is 750 words", "point at a spec file in the repository")
    ]


def test_lint_items_returns_empty_list_for_a_clean_dag():
    # work-shape.md §8: "a clean DAG yields `[]`."
    items = [{"task": "t1", "phase": "build", "surfaces": ["agent_tools/route.py"], "body": "plain ticket body"}]
    assert route.lint_items(items, "acme/widgets", ()) == []


def _cross_item(body="", surfaces=()):
    return [{"task": "t1", "phase": "build", "surfaces": list(surfaces), "body": body}]


def test_lint_items_flags_a_file_under_another_repositorys_package():
    problems = route.lint_items(_cross_item("edit harness/cli.py"), "/home/x/coxswain-tools", ())
    assert problems == [route.Problem(
        "t1", "cross_repo", "names harness/cli.py, which lives in coxswain-graphs",
        "paste the code the build needs into the ticket: a build reads only its own repository",
    )]


def test_lint_items_does_not_flag_a_file_in_its_own_repository():
    items = _cross_item("edit agent_tools/run_store.py", ["agent_tools/run_store.py"])
    assert route.lint_items(items, "/home/x/coxswain-tools", ()) == []


def test_lint_items_stands_cross_repo_down_for_an_unknown_or_missing_repo():
    items = _cross_item("edit harness/cli.py")
    assert route.lint_items(items, "/home/x/widgets", ()) == []
    assert route.lint_items(items, None, ()) == []


def test_lint_items_never_counts_tests_or_docs_as_cross_repo():
    items = _cross_item("see docs/design/x.md", ["tests/test_x.py"])
    assert route.lint_items(items, "/home/x/coxswain-tools", ()) == []


def test_lint_items_reports_one_cross_repo_problem_for_a_path_named_twice():
    items = _cross_item("edit `harness/cli.py`, then harness/cli.py again", ["harness/cli.py"])
    problems = route.lint_items(items, "/home/x/coxswain-tools/", ())
    assert [p.rule for p in problems] == ["cross_repo"]


def _ticket(id, title, *, state="ready", phase="build", surfaces=(), needs=(), body=""):
    return {
        "id": id,
        "title": title,
        "state": state,
        "phase": phase,
        "surfaces": list(surfaces),
        "needs": list(needs),
        "body": body,
    }


def test_merge_same_phase_merges_two_ready_tickets_sharing_one_surface():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"], body="Body A"),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py"], body="Body B"),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items == [
        {
            "id": "a", "title": "Add A; Add B", "state": "ready", "phase": "build",
            "surfaces": ["agent_tools/x.py"], "needs": [],
            "body": "## Add A\nBody A\n\n## Add B\nBody B",
        },
        {
            "id": "b", "title": "Add B", "state": "dropped", "phase": "build",
            "surfaces": ["agent_tools/x.py"], "needs": [], "body": "Body B",
            "merged_into": "a",
        },
    ]
    assert merges == [{"members": ["a", "b"], "into": "a", "surfaces": ["agent_tools/x.py"]}]


def test_merge_same_phase_merges_a_chain_through_an_intermediate_ticket():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py", "agent_tools/y.py"]),
        _ticket("c", "Add C", surfaces=["agent_tools/y.py"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert len(result_items) == 3
    merged = result_items[0]
    assert merged["id"] == "a"
    assert merged["title"] == "Add A; Add B; Add C"
    assert merged["surfaces"] == ["agent_tools/x.py", "agent_tools/y.py"]
    assert [item["state"] for item in result_items[1:]] == ["dropped", "dropped"]
    assert [item["merged_into"] for item in result_items[1:]] == ["a", "a"]
    assert merges == [{
        "members": ["a", "b", "c"], "into": "a",
        "surfaces": ["agent_tools/x.py", "agent_tools/y.py"],
    }]


def test_merge_same_phase_leaves_different_phase_tickets_untouched():
    items = [
        _ticket("a", "Add A", phase="build", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", phase="test", surfaces=["agent_tools/x.py"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items == items
    assert merges == []


def test_merge_same_phase_never_merges_a_done_ticket():
    items = [
        _ticket("a", "Add A", state="done", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", state="ready", surfaces=["agent_tools/x.py"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items == items
    assert merges == []


def test_merge_same_phase_rewrites_an_external_needs_reference_to_the_merged_id():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py"]),
        _ticket("c", "Add C", phase="test", needs=["b"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items[2]["needs"] == ["a"]


def test_merge_same_phase_leaves_disjoint_same_phase_tickets_untouched():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", surfaces=["agent_tools/y.py"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items == items
    assert merges == []


def _lint_ns(initiative_dir, repo=None):
    return argparse.Namespace(initiative_dir=str(initiative_dir), repo=repo)


def test_route_lint_cli_resolves_repo_from_initiative_frontmatter(tmp_path, capsys):
    # 2026-09-09 revise: `--repo` defaults to None, and the CLI handler
    # falls back to the filed initiative's own `repo:` field, so the
    # default invocation does not flag every path as unreachable.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "widget-fix.md").write_text(
        '---\nid: "widget-fix"\nphase: "build"\nsurfaces: []\n---\nsee ~/notes/plan.md for context\n',
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 2
    assert "widget-fix reach: '~/notes/plan.md' is outside 'acme/widgets' -> " in out


def test_route_lint_cli_stands_reach_down_and_says_so_once_with_no_repo_anywhere(tmp_path, capsys):
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\n---\nfix the widget\n', encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "widget-fix.md").write_text(
        '---\nid: "widget-fix"\nphase: "build"\nsurfaces: []\n---\nsee ~/notes/plan.md for context\n',
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == f"routing: no repo found for {initiative}; reach check skipped\n"


def test_route_lint_cli_merges_same_phase_ready_tickets_before_linting(tmp_path, capsys):
    # same-phase-tickets-that-share-a-file-are-merged-wire-merge-into-lint-and-launch:
    # merge_same_phase runs first, so a mergeable pair never reaches the
    # coupling check as two separate tickets.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    t1 = build / "t1.md"
    t2 = build / "t2.md"
    attempt = "attempts:\n  - run: widget-fix-1\n    ts: '2026-09-01'\n"
    t1.write_text(
        "---\nid: t1\ntitle: First\nphase: build\nstate: ready\nsurfaces: [tests/test_widget.py]\n"
        f"budget_usd: 2.0\n{attempt}lint: []\n---\nfirst ticket\n",
        encoding="utf-8",
    )
    t2.write_text(
        "---\nid: t2\ntitle: Second\nphase: build\nstate: ready\nsurfaces: [tests/test_widget.py]\n"
        f"budget_usd: 3.0\n{attempt}---\nsecond ticket\n",
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == "merged: t1 + t2 -> t1 (shared tests/test_widget.py)\n"
    merged_text = t1.read_text(encoding="utf-8")
    merged_fields, _ = route.parse_frontmatter(merged_text)
    assert merged_fields["state"] == "ready"
    assert merged_fields["title"] == "First; Second"
    assert f"budget_usd: 2.0\n{attempt}lint: []\n" in merged_text
    dropped_text = t2.read_text(encoding="utf-8")
    dropped_fields, _ = route.parse_frontmatter(dropped_text)
    assert dropped_fields["state"] == "dropped"
    assert dropped_fields["merged_into"] == "t1"
    assert f"budget_usd: 3.0\n{attempt}" in dropped_text


def test_route_lint_cli_does_not_flag_coupling_when_a_needs_edge_orders_the_pair(tmp_path, capsys):
    # tools-loop-fixes-lint-needs: with no explicit `state:`, `_route_lint`
    # defaults both tickets to `todo`, so they are still merge-eligible and
    # the merge runs before any coupling check would otherwise see them.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "t1.md").write_text(
        "---\nid: t1\nphase: build\nsurfaces: [tests/test_widget.py]\n---\nfirst ticket\n",
        encoding="utf-8",
    )
    (build / "t2.md").write_text(
        "---\nid: t2\nphase: build\nsurfaces: [tests/test_widget.py]\nneeds: [t1]\n---\nsecond ticket\n",
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == "merged: t1 + t2 -> t1 (shared tests/test_widget.py)\n"


def test_route_lint_cli_refuses_a_needs_edge_inside_one_phase(tmp_path, capsys):
    # lint-refuses-a-needs-edge-inside-one-phase: disjoint surfaces, so no merge
    # folds the pair; the same-phase needs edge alone must exit 2.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "t1.md").write_text(
        "---\nid: t1\nphase: build\nsurfaces: [agent_tools/x.py]\n---\nfirst ticket\n",
        encoding="utf-8",
    )
    (build / "t2.md").write_text(
        "---\nid: t2\nphase: build\nsurfaces: [agent_tools/y.py]\nneeds: [t1]\n---\nsecond ticket\n",
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 2
    assert out == (
        "t2 phase_needs: t2 needs t1 in the same phase 'build'"
        " -> move it to a later phase that needs this one\n"
    )


def test_parse_profile_reads_the_weekly_ceiling_beside_the_window_ceiling():
    text = VALID_PROFILE + "spend:\n  window_ceiling_usd: 50\n  weekly_ceiling_usd: 1043\n"
    profile = route.parse_profile(text)
    assert profile["window_ceiling_usd"] == 50.0
    assert profile["weekly_ceiling_usd"] == 1043.0


def test_parse_profile_reads_sources_and_repo_map_as_json_values():
    text = VALID_PROFILE + 'sources: {"github": {"repos": ["a/b"]}}\nrepo_map: {"a/b": "tools"}\n'
    profile = route.parse_profile(text)
    assert profile["sources"] == {"github": {"repos": ["a/b"]}}
    assert profile["repo_map"] == {"a/b": "tools"}


def _cand(link: str, repo: str = "a/b"):
    from agent_tools.sources import Candidate

    return Candidate(title="Fix it", body="b", repo=repo, link=link)


def test_pull_plan_writes_a_mapped_candidate_with_source_and_link():
    files, reviews, refusals = route.pull_plan([_cand("l1")], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert refusals == []
    assert files == [route.intake_file("Fix it", "b", "tools", "2026-09-24", source="github", link="l1")]
    assert "link: l1\n" in files[0]["intake/2026-09-24-fix-it.md"]


def test_pull_plan_skips_a_taken_link_and_refuses_an_unmapped_repo_by_name():
    cands = [_cand("l1"), _cand("l2", repo="x/y")]
    files, reviews, refusals = route.pull_plan(cands, frozenset({"l1"}), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert files == []
    assert len(refusals) == 1 and "'Fix it'" in refusals[0] and "l2" in refusals[0] and "x/y" in refusals[0]


def test_pull_plan_routes_a_pr_to_a_review_and_an_issue_to_an_intake_file():
    from agent_tools.sources import Candidate

    pr = Candidate(title="Add retry", body="b", repo="a/b", link="l2", kind="pr")
    files, reviews, refusals = route.pull_plan([_cand("l1"), pr], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert (len(files), reviews, refusals) == (1, ["l2"], [])


def test_pull_plan_with_ids_sequence_writes_the_allocated_id(monkeypatch):
    monkeypatch.setattr(route, "allocate_intake_id", lambda: "I7")
    files, reviews, refusals = route.pull_plan(
        [_cand("l1")], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github", ids="sequence"
    )
    assert refusals == []
    assert set(files[0]) == {"intake/I7.md"}


def test_parse_allocate_id_returns_the_id_on_a_clean_exit():
    assert route.parse_allocate_id(0, '{"id": "I7"}') == "I7"


def test_parse_allocate_id_raises_naming_the_exit_code_and_output_on_failure():
    with pytest.raises(ValueError, match=r"id allocation failed \(exit 1\): boom"):
        route.parse_allocate_id(1, "boom")


def test_allocate_intake_id_raises_when_the_harness_python_is_unavailable(monkeypatch):
    monkeypatch.setattr(route.run_store, "_harness_python", lambda: None)
    with pytest.raises(ValueError, match="id allocation failed"):
        route.allocate_intake_id()


def test_parse_profile_keeps_spend_weekly_reset_as_text():
    text = VALID_PROFILE + 'spend:\n  weekly_ceiling_usd: 1430\n  weekly_reset: "Sun 04:00 America/New_York"  # the meter\n'
    profile = route.parse_profile(text)
    assert (profile["weekly_ceiling_usd"], profile["weekly_reset"]) == (1430.0, "Sun 04:00 America/New_York")


def test_parse_profile_reads_an_unquoted_weekly_reset():
    assert route.parse_profile(VALID_PROFILE + "spend:\n  weekly_reset: Sun 04:00 America/New_York\n")["weekly_reset"] == "Sun 04:00 America/New_York"

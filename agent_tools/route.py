"""Pure core for the routing layer: profile parsing and naming, plus the one thin edge `cox route file`
writes a file through. Every function here but `write_filed_item` and `set_initiative_priority` takes plain
arguments and returns plain values, with no file reads and no env access. `set_initiative_priority` is a
second edge that writes through `write_filed_item`. `write_filed_item` is the exception the name says it is: it
decides through two pure helpers (`_unrecorded_row_warning`, `_filed_write_error`) what to print, and does
no more I/O than the one store call and the one file write the ticket asks it to order."""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from agent_tools import chair, queue_rows, run_store
from agent_tools.intake_state import intake_group
from agent_tools.pacing import Assessment

__all__ = [
    "Problem",
    "ProfileError",
    "allocate_intake_id",
    "child_env",
    "context_document",
    "context_from_rows",
    "context_rows",
    "current_priority",
    "harness_argv",
    "initiative_files",
    "initiative_summaries",
    "intake_entries",
    "intake_file",
    "latest_groups_file",
    "launch_blockers",
    "launch_claim",
    "launch_claim_gate",
    "launch_gate",
    "lint_items",
    "merge_same_phase",
    "next_priority",
    "next_run_id",
    "overlay",
    "parse_allocate_id",
    "parse_frontmatter",
    "parse_pid",
    "parse_profile",
    "priority_text",
    "pull_plan",
    "render_context",
    "render_status",
    "review_argv",
    "run_entries",
    "run_under_claim",
    "set_initiative_priority",
    "slugify",
    "state_problems",
    "status_entries",
    "status_rows",
    "surface_candidates",
    "work_item",
    "write_filed_item",
]

_PROFILE_FIELDS = (
    "team",
    "cartridges_dir",
    "skills_roots",
    "provider_profile",
    "harness_dir",
    "workspace_dir",
    "assume",
)

_KNOWN_KEYS = {
    "team",
    "cartridges_dir",
    "skills_roots",
    "provider_profile",
    "harness_dir",
    "workspace_dir",
    "assume",
    "router",
    "sources",
    "repo_map",
    "forge",
    "tracker",
    "umbrella_dir",
    "log_retention_days",  # run logs and traces kept locally, in days; run_logs.DEFAULT_RETENTION_DAYS when absent
    "ids",  # `slug` (default) or `sequence`; `intake_file`'s own id source, spec's `route file --intake`
}

_JSON_KEYS = {"sources", "repo_map"}

_SPEND_KEYS = {"window_ceiling_usd", "weekly_ceiling_usd", "node_cap_usd"}
_SPEND_TEXT_KEYS = {"weekly_reset"}  # `Sun 04:00 America/New_York`, parsed by usage_window.parse_weekly_reset

_CHAIR_KEYS = {"housekeeping_hours", "stale_days", "idle_stall_minutes"}  # every key `cli._chair_run_deps` reads off `profile["chair"]`


class ProfileError(Exception):
    """A profile file line is nested, unknown, or otherwise unparsable."""


def _stripped_content(line: str) -> str:
    # An inline `#` (preceded by whitespace, per spec §1's own sample
    # `assume: a          # gate answer ...`) starts a trailing comment;
    # strip it before splitting key/value so it never lands in a value.
    comment = re.search(r"(?<=\s)#", line)
    return line[: comment.start()].rstrip() if comment else line


def parse_profile(text: str) -> dict:
    """Parse the flat `key: scalar` / `key: [a, b]` YAML subset in spec §1,
    plus two nested blocks: a bare `spend:` line followed by indented
    `window_ceiling_usd:`/`weekly_ceiling_usd:`/`node_cap_usd:` lines, all optional, parsed as
    floats onto the flat result, and a `weekly_reset:` line kept as text, quotes stripped;
    and a bare `chair:` line followed by indented `housekeeping_hours:`/`stale_days:`/`idle_stall_minutes:` lines,
    all optional, parsed as floats into `result["chair"]` (every key `cli._chair_run_deps`
    reads off `profile["chair"]`; `log_retention_days` stays a flat top-level key, not one of
    these).

    A nested key outside a `spend:` or `chair:` block, an unrecognized key inside one,
    or a key outside the known set raises ProfileError naming the offending
    line (number + text). `assume` defaults to 'a' when absent.
    """
    result: dict = {}
    in_spend = False
    in_chair = False
    in_hosts = False  # `lane_hosts:` is a list of mappings that cli reads from the YAML text; its lines are skipped here
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        if in_hosts and line != line.lstrip():
            continue
        if line != line.lstrip():
            if in_chair:
                content = _stripped_content(line)
                if ":" not in content:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                key, _, value = content.partition(":")
                key, value = key.strip(), value.strip()
                if key not in _CHAIR_KEYS:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                try:
                    result.setdefault("chair", {})[key] = float(value)
                except ValueError:
                    raise ProfileError(f"line {lineno}: {raw_line}") from None
                continue
            if not in_spend:
                raise ProfileError(f"line {lineno}: {raw_line}")
            content = _stripped_content(line)
            if ":" not in content:
                raise ProfileError(f"line {lineno}: {raw_line}")
            key, _, value = content.partition(":")
            key, value = key.strip(), value.strip()
            if key in _SPEND_TEXT_KEYS:
                text_value = value.strip("\"'")
                if not text_value:
                    raise ProfileError(f"line {lineno}: {raw_line}")
                result[key] = text_value
                continue
            if key not in _SPEND_KEYS:
                raise ProfileError(f"line {lineno}: {raw_line}")
            try:
                result[key] = float(value)
            except ValueError:
                raise ProfileError(f"line {lineno}: {raw_line}") from None
            continue
        in_spend = False
        in_chair = False
        in_hosts = False
        content = _stripped_content(line)
        if ":" not in content:
            raise ProfileError(f"line {lineno}: {raw_line}")
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "spend":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_spend = True
            continue
        if key == "chair":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_chair = True
            continue
        if key == "lane_hosts":
            if value:
                raise ProfileError(f"line {lineno}: {raw_line}")
            in_hosts = True
            continue
        if key not in _KNOWN_KEYS:
            raise ProfileError(f"line {lineno}: {raw_line}")
        if key in _JSON_KEYS:
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                raise ProfileError(f"line {lineno}: {raw_line}") from None
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [item.strip() for item in inner.split(",")] if inner else []
            result[key] = items
        else:
            result[key] = value
    # An `assume:` with no value is treated the same as an absent key, not
    # as a literal empty string, so it never poisons the harness argv.
    if result.get("assume") == "":
        result.pop("assume")
    result.setdefault("assume", "a")
    return result


_SLUG_STOPWORDS = {"the", "a", "an", "cox", "fix", "loop", "initiative"}


def slugify(title: str) -> str:
    """Lower-case, non-alphanumerics collapsed to '-'. Longer than 48 chars,
    leading stopwords (`the`, `a`, `an`, `cox`, `fix`, `loop`, `initiative`)
    are dropped and the result is cut at the last word boundary under the
    cap, never mid-word; otherwise returned as-is.

    A title with no alphanumeric characters (all punctuation/symbols)
    slugifies to the empty string; a caller that uses the result as a path
    segment (spec §3, `work/<slug>/...`) must guard against that itself —
    this pure core does not refuse or substitute.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if len(slug) <= 48:
        return slug
    words = [w for w in slug.split("-") if w]
    while len(words) > 1 and words[0] in _SLUG_STOPWORDS:
        words = words[1:]
    slug = "-".join(words)
    if len(slug) <= 48:
        return slug
    cut = slug[:48]
    boundary = cut.rfind("-")
    return (cut[:boundary] if boundary > 0 else cut).strip("-")


def next_run_id(existing_names, prefix: str) -> str:
    """Return `<prefix>-<n>` for one past the highest n used by a name in
    existing_names with that prefix, or `<prefix>-1` when there is none.

    It never fills a gap: a gap may be a run whose files were archived while
    its store rows remain, and reusing that id would collide with the rows.

    Spec §4/§5 fill `existing_names` from a real `runs_dir` listing, whose
    entries are `<run-id>.log` and `<run-id>.pid`, not bare ids — so the
    pattern must tolerate an optional trailing extension. Matching bare ids
    only would let `next_run_id` hand back an id already in use, appending
    to a live run's log and clobbering its pidfile.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\.\w+)?$")
    taken = set()
    for name in existing_names:
        match = pattern.match(name)
        if match:
            taken.add(int(match.group(1)))
    return f"{prefix}-{max(taken, default=0) + 1}"


class _Raw(str):
    """Marks a frontmatter value this module wrote itself as a literal YAML
    fragment (`needs: []`, `surfaces: []`) that must be emitted verbatim.

    Never wrap a caller-supplied string in this: a value's *shape* is not
    evidence of its meaning. An earlier version of `_yaml_scalar` treated
    any bracketed value as a raw list, which meant a title like
    `[draft] ship it` was emitted bare as `title: [draft] ship it` — a real
    YAML reader parses that as a flow sequence, not the string it looks
    like. Only the caller who wrote the literal can know it is one; that
    is what wrapping it in `_Raw` at the call site records.
    """


def _yaml_scalar(value: str) -> str:
    """Quote a frontmatter value that would otherwise read as something
    other than the plain string it is.

    Wrong belief this guards against: that emitting `key: {value}` verbatim
    is safe because a title or repo is "just text". A title with a colon
    (this very task's own title, `route.py: initiative_files`) or a `#`
    turns `title: route.py: initiative_files` into a line a real YAML
    reader parses as a nested mapping or a truncated comment, not the
    string it looks like. `_Raw` values pass through unquoted; every other
    value is a scalar regardless of what it looks like.
    """
    if isinstance(value, _Raw):
        return str(value)
    starts_like_yaml_flow = value.startswith("[") or value.startswith("{")
    # `'` is quoted for the same reason as `"`: a value that already looks
    # quoted must not be handed to a YAML reader bare. This half has no
    # round-trip test below. `parse_frontmatter` has no single-quoted value
    # form, so a title starting with `'` parses as the bare string whether
    # or not this line quotes it, and a test that failed on its removal
    # would have to assert the writer's output shape, which this task
    # rules out.
    starts_like_a_quote = value.startswith('"') or value.startswith("'")
    if (
        value == ""
        or ":" in value
        or "#" in value
        or value != value.strip()
        or starts_like_yaml_flow
        or starts_like_a_quote
    ):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _frontmatter(fields, body: str) -> str:
    """Render `---`-delimited frontmatter followed by a body, spec §3.

    `fields` is an ordered iterable of (key, value) pairs; each value is
    quoted by `_yaml_scalar` when left bare it would change meaning (a
    colon, a `#`, surrounding whitespace, or emptiness), never otherwise.
    """
    header = "\n".join(f"{key}: {_yaml_scalar(value)}" for key, value in fields)
    return f"---\n{header}\n---\n\n{body}\n"


def _slug_or_raise(title: str) -> str:
    """`slugify`'s own docstring hands the empty-slug case to any caller
    that uses the result as a path segment (spec §3, `work/<slug>/...`).
    A title of pure punctuation would otherwise slugify to `""`, and
    `work/{slug}/...` would collapse to `work/initiative.md` — a write to
    the top of the work store, past spec §3's existing-path refusal, since
    that path is genuinely new. This is the guard slugify asked for.
    """
    slug = slugify(title)
    if not slug:
        raise ValueError(f"title {title!r} has no alphanumeric characters to slugify")
    return slug


def initiative_files(
    title: str,
    body: str,
    repo: str,
    phase: str = "build",
    surfaces: list[str] | None = None,
    budget_usd: float | None = None,
    slug: str | None = None,
) -> dict:
    """Content for a one-task initiative (spec §3, `route file` without
    `--intake`): `work/<slug>/initiative.md` and
    `work/<slug>/<phase>/<slug>.md`, keyed by path relative to the work
    store. `body` falls back to `title` when empty. No filesystem writes
    happen here — the caller applies the mapping.

    `slug`, given, is normalized through the same guard as `title` and used
    for the id and both paths in its place — an intake's own `slug:` field,
    for instance, is never trusted raw as a path segment. `surfaces`
    defaults to empty (unscoped); each entry is quoted the same way any
    other frontmatter scalar is, so a path carrying `:` or `#` round-trips.
    `budget_usd`, given, also adds `attempts: []` and `lint: []`; omitted,
    none of the three appear.
    """
    slug = _slug_or_raise(slug or title)
    text = body if body else title
    initiative_text = _frontmatter(
        [("id", slug), ("title", title), ("repo", repo)], text
    )
    surfaces_raw = _Raw("[" + ", ".join(_yaml_scalar(s) for s in surfaces) + "]") if surfaces else _Raw("[]")
    task_fields = [
        ("id", slug),
        ("phase", phase),
        ("state", "ready"),
        ("needs", _Raw("[]")),
        ("surfaces", surfaces_raw),
        ("title", title),
    ]
    if budget_usd is not None:
        task_fields += [
            ("budget_usd", str(budget_usd)),
            ("attempts", _Raw("[]")),
            ("lint", _Raw("[]")),
        ]
    task_text = _frontmatter(task_fields, text)
    return {
        f"work/{slug}/initiative.md": initiative_text,
        f"work/{slug}/{phase}/{slug}.md": task_text,
    }


def initiative_text(id: str, title: str, repo: str, intake: str, body: str) -> str:
    """`work/<id>/initiative.md` for `route launch decompose` (spec §3),
    written from data the launcher already has rather than proposed by a
    seat: the idea's `title`/`repo` and its path relative to the workspace
    as `intake`, with the idea's own body carried through unchanged.
    """
    return _frontmatter([("id", id), ("title", title), ("repo", repo), ("intake", intake)], body)


def _allocate_id_argv(python: str) -> list[str]:
    """argv for the harness id allocation, the `python -m harness.<module>` shape `run_store` uses."""
    return [python, "-m", "harness.store_ids", "allocate", "--json"]


def parse_allocate_id(code: int, stdout: str) -> str:
    """The `id` of exit 0 with a JSON object holding a non-empty string `id`; otherwise raises naming the exit and output."""
    parsed = None
    if code == 0:
        try:
            parsed = json.loads(stdout)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("id"), str) and parsed["id"]:
        return parsed["id"]
    raise ValueError(f"id allocation failed (exit {code}): {stdout.strip() or 'no JSON object on stdout'}")


def allocate_intake_id() -> str:
    """Edge: `python -m harness.store_ids allocate --json`; raises when the harness python is missing or the call fails."""
    python = run_store._harness_python()
    if python is None:
        raise ValueError("id allocation failed: harness python is not available")
    try:
        done = subprocess.run(_allocate_id_argv(str(python)), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"id allocation failed to run: {exc}") from exc
    return parse_allocate_id(done.returncode, done.stdout or done.stderr)


def intake_file(
    title: str,
    body: str,
    repo: str,
    date: str,
    *,
    source: str = "",
    link: str = "",
    ids: str = "slug",
    allocate: Callable[[], str] | None = None,
) -> dict:
    """Content for `route file --intake` (spec §3):
    `intake/<date>-<slug>.md`, same frontmatter shape as the initiative
    file. `date` arrives as a string (e.g. `2026-09-03`) so this stays
    pure — no clock reads here. A pulled file also carries `source` and `link`.

    Under `ids="sequence"` the file is `intake/<id>.md` with the id from `allocate`; a failed allocation raises, never slugifies.
    """
    text = body if body else title
    origin = [("source", source), ("link", link)] if link else []
    if ids == "sequence":
        id_ = (allocate or allocate_intake_id)()
        return {f"intake/{id_}.md": _frontmatter([("id", id_), ("title", title), *origin], text)}
    slug = _slug_or_raise(title)
    file_text = _frontmatter([("id", slug), ("title", title), ("repo", repo), *origin], text)
    return {f"intake/{date}-{slug}.md": file_text}


def pull_plan(
    candidates: Sequence,
    taken_links: frozenset[str],
    profile_repos: Mapping[str, str],
    *,
    date: str,
    source: str,
    ids: str = "slug",
) -> tuple[list[dict], list[str], list[str]]:
    """Intake files, PR links to review, and refusals; an unmapped repo is refused, never guessed, and a second candidate on one path is refused, never dropped."""
    reviews = [c.link for c in candidates if c.kind == "pr"]
    fresh = [c for c in candidates if c.kind != "pr" and c.link not in taken_links]
    unmapped = [_pull_refusal(c, f"repo {c.repo} has no mapping in the profile") for c in fresh if c.repo not in profile_repos]
    mapped = [c for c in fresh if c.repo in profile_repos]
    unnamed = [_pull_refusal(c, "title has no alphanumeric characters to slugify") for c in mapped if not slugify(c.title)]
    planned = [
        (c, intake_file(c.title, c.body, profile_repos[c.repo], date, source=source, link=c.link, ids=ids))
        for c in mapped
        if slugify(c.title)
    ]
    paths = [next(iter(mapping)) for _, mapping in planned]
    first = [paths.index(path) == i for i, path in enumerate(paths)]
    clashes = [
        _pull_refusal(c, f"{path} is already planned for another candidate")
        for (c, _), path, is_first in zip(planned, paths, first)
        if not is_first
    ]
    files = [mapping for (_, mapping), is_first in zip(planned, first) if is_first]
    return files, reviews, unmapped + unnamed + clashes


def review_argv(link: str, profile: str) -> list[str]:
    return ["cox", "runs", "review", "--pr", link, "--profile", profile]


def _pull_refusal(candidate, reason: str) -> str:
    return f"routing: refusing {candidate.title!r} ({candidate.link}): {reason}"


def link_intake(initiative_text: str, intake_text: str, intake_path: str, initiative_id: str) -> tuple[str, str]:
    """`initiative_text`'s `intake` field becomes `intake_path`; `intake_text`'s `initiative` field becomes `initiative_id`; a rerun updates the value in place rather than duplicating the key."""
    initiative_fields, initiative_body = parse_frontmatter(initiative_text)
    intake_fields, intake_body = parse_frontmatter(intake_text)
    new_initiative = _frontmatter({**initiative_fields, "intake": intake_path}.items(), initiative_body)
    new_intake = _frontmatter({**intake_fields, "initiative": initiative_id}.items(), intake_body)
    return new_initiative, new_intake


_BACKTICKED = re.compile(r"`([^`\s]+)`")


def surface_candidates(body: str, repo: str) -> list[str]:
    """Backticked tokens in `body` that look like a path under `repo` — a
    `/` or a recognizable file suffix, no whitespace, no `scheme://` — in
    first-seen order, de-duplicated. Pure: this never touches a filesystem,
    so a token that merely looks like a path but names nothing real is
    still a candidate; the caller checks that.
    """
    found = []
    for token in _BACKTICKED.findall(body):
        if "://" in token or token in found:
            continue
        if "/" in token or re.search(r"\.[A-Za-z0-9]{1,8}$", token):
            found.append(token)
    return found


def harness_argv(profile: dict, graph: str, run_id: str, **needs) -> list:
    """Build the harness command line, spec §4, from a parsed `profile`
    and the graph-specific `needs` (`initiative`/`repo` for `epic`, plus
    `task` for `rescue`; `idea`/`initiative_id`, plus an optional `task_ids`, for `decompose`). Pure: no Popen, no env reads.
    """
    harness_dir = profile["harness_dir"]
    workspace_dir = profile["workspace_dir"]
    argv = [
        f"{harness_dir}/.venv/bin/python",
        f"{harness_dir}/shell.py",
        graph,
        "--team",
        profile["team"],
        "--cartridges-dir",
        profile["cartridges_dir"],
    ]
    for root in profile.get("skills_roots", []):
        argv += ["--skills-root", root]
    argv += [
        "--provider-profile",
        profile["provider_profile"],
        "--runs-dir",
        f"{workspace_dir}/runs",
        "--assume",
        profile["assume"],
        "--run-id",
        run_id,
    ]
    if graph in ("epic", "rescue"):
        argv += ["--initiative", needs["initiative"], "--repo", needs["repo"]]
        if graph == "rescue":
            argv += ["--task", needs["task"]]
        if needs.get("fix_attempts") is not None:
            argv += ["--fix-attempts", str(needs["fix_attempts"])]
    elif graph == "decompose":
        argv += ["--idea", needs["idea"], "--initiative-id", needs["initiative_id"]]
        if needs.get("task_ids"):
            argv += ["--task-ids", needs["task_ids"]]
    elif graph == "cos":
        # `_KNOWN_KEYS` carries no `max_parallel` field, so a profile can
        # never supply one; the bound is the literal default until the
        # profile schema grows a key for it.
        argv += ["--max-parallel", "3"]
    argv += ["--workdir", workspace_dir]
    if "node_cap_usd" in profile:
        argv += ["--node-cap-usd", str(profile["node_cap_usd"])]
    return argv


def build_sweep_argv(idea: str, initiative_id: str, label: str | None = None) -> list:
    """`cox route launch sweep --idea <file> --initiative-id <id>`, work-shape.md §1."""
    argv = ["sweep", "--idea", idea, "--initiative-id", initiative_id]
    if label is not None:
        argv += ["--label", label]
    return argv


class Problem(NamedTuple):
    """work-shape.md §3: one static-lint finding."""

    task: str
    rule: str
    detail: str
    fix: str


# The character before an unsafe path may be whitespace, start-of-string, a
# backtick, an opening parenthesis or a quote — this project's own house
# style wraps every file reference in backticks, so a bare `\s` boundary
# misses a real out-of-repo path inside a code span.
_UNSAFE_PATH = re.compile(r"(?:^|[\s\x60\x28\x27\x22])(/[\w./-]+|workspace/[\w./-]*|~/[\w./-]*)")
_DISALLOWED_COMMANDS = ("cox", "uv", "gh", "git push", "ruff")
# Top-level directories a real absolute path starts with (Linux FHS, macOS, Nix), as graphs' ticket_lint
# `_FS_ROOTS`: a rooted token whose first segment is none of these (`/v1/chat/completions`) is a route, not a file.
_FS_ROOTS = frozenset({
    "bin", "boot", "etc", "home", "lib", "lib64", "media", "mnt", "nix", "opt", "private", "proc", "root", "run",
    "sbin", "snap", "srv", "sys", "tmp", "Users", "usr", "var", "Volumes",
})


# Top-level directories only one Coxswain repository has, by repository basename. `docs/` and `tests/` exist in
# every repository, so they are deliberately absent: naming one is never a cross-repository reference.
_REPO_ROOTS: dict[str, frozenset[str]] = {
    "coxswain-tools": frozenset({"agent_tools"}),
    "coxswain-graphs": frozenset({"harness", "runner", "graphs"}),
    "coxswain-cartridges": frozenset({"core", "providers", "cartridges", "skills-plugins"}),
    "coxswain": frozenset({"devtools"}),
    "coxswain-plugins": frozenset({"coxswain_plugins"}),
}
_PATH_EDGE = "\x60\x27\x22(),."


def _is_route(path: str) -> bool:
    return path.startswith("/") and path.split("/")[1] not in _FS_ROOTS


def _inside_repo(path: str, repo: str) -> bool:
    """§3 excludes a path only when it is inside `repo`; wrong belief this
    guards: that any absolute/workspace/~ path is automatically reach-unsafe.
    A path is a prefix match on `repo` (a filesystem root, not a slug) —
    plain-data comparison, no filesystem read.
    """
    root = repo.rstrip("/") if repo else ""
    return bool(root) and (path == root or path.startswith(root + "/"))


def _cross_repo_paths(text: str, repo: str | None) -> list[tuple[str, str]]:
    """`(path, owning repository)` for each relative token under another repository's own top-level directory.
    Stands down when `repo` is unset or its basename is not in `_REPO_ROOTS`. Order of first sight, no repeats."""
    home = os.path.basename(repo.rstrip("/")) if repo else ""
    if home not in _REPO_ROOTS:
        return []
    owner = {top: name for name, tops in _REPO_ROOTS.items() if name != home for top in tops}
    paths = dict.fromkeys(t.strip(_PATH_EDGE) for t in text.split())
    return [(p, owner[p.split("/")[0]]) for p in paths if "/" in p and p.split("/")[0] in owner]


def _item_problems(item: dict, repo: str | None, grants) -> list:
    """reach, grant, size and cross_repo, each scoped to one ticket item. `repo` falsy
    (no repo could be resolved) stands the reach rule down instead of
    flagging every path — there is no target to check against. An unknown
    repo basename stands cross_repo down for the same reason."""
    task, body = item["task"], item.get("body", "")
    text = body + " " + " ".join(item.get("surfaces", []))
    allowed = set(grants or ())
    reach = [
        Problem(task, "reach", f"{p!r} is outside {repo!r}",
                "move the artifact into the repository or drop the reference")
        for p in _UNSAFE_PATH.findall(text)
        if not _inside_repo(p, repo) and not _is_route(p)
    ] if repo else []
    grant = [
        Problem(task, "grant", f"command {cmd!r} is not in this role's grant",
                "name only pytest, git status, git diff")
        for cmd in _DISALLOWED_COMMANDS
        if cmd not in allowed and re.search(rf"(?<!\w){re.escape(cmd)}(?!\w)", body)
    ]
    words = len(body.split())
    size = [Problem(task, "size", f"body is {words} words",
                     "point at a spec file in the repository")] if words > 700 else []
    cross_repo = [
        Problem(task, "cross_repo", f"names {path}, which lives in {other}",
                "paste the code the build needs into the ticket: a build reads only its own repository")
        for path, other in _cross_repo_paths(text, repo)
    ]
    return reach + grant + size + cross_repo


def _coupling_problems(items) -> list:
    """Two tickets in one phase sharing any `surfaces` entry.

    `merge_same_phase` already folds a same-phase `ready`/`todo` pair that
    shares a surface into one ticket before this runs, so a hit here means
    at least one side is `approved`, `in_progress`, or `done` (excluded
    from merging) and still collides with another ticket in its phase — a
    `needs` edge between the two no longer excuses it.

    §3's coupling rule has a second clause — "or whose named modules
    import one another" — not checked here. Wrong belief to avoid: that
    this function covers coupling in full; `items` carries surface paths,
    not import graphs, so the import clause is undetected.
    """
    by_phase: dict = {}
    for item in items:
        by_phase.setdefault(item.get("phase"), []).append(item)
    problems = []
    for group in by_phase.values():
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                shared = sorted(
                    s for s in first.get("surfaces", [])
                    if s in second.get("surfaces", [])
                )
                if shared:
                    problems.append(Problem(
                        first["task"], "coupling",
                        f"{first['task']} and {second['task']} both surface {shared[0]!r}",
                        "merge them, or move one to a later phase",
                    ))
    return problems


def _phase_needs_problems(items) -> list:
    """A `needs` edge between two tickets sharing one `phase` always deadlocks a phase land.

    A `needs` id absent from `items` is skipped. Wrong belief to avoid: that
    some other rule catches it; `reach` checks paths only, and no rule here
    checks that a `needs` id names a real ticket.
    """
    phase_by_task = {item["task"]: item.get("phase") for item in items if "task" in item}
    return [
        Problem(
            item["task"], "phase_needs",
            f"{item['task']} needs {other} in the same phase {item.get('phase')!r}",
            "move it to a later phase that needs this one",
        )
        for item in items
        for other in item.get("needs", [])
        if other in phase_by_task and phase_by_task[other] == item.get("phase")
    ]


def lint_items(items, repo: str | None, grants) -> list:
    """work-shape.md §3: reach, grant, size, cross_repo and coupling over a decomposed
    DAG's parsed ticket items; no model, no I/O."""
    problems = [p for item in items for p in _item_problems(item, repo, grants)]
    return problems + _coupling_problems(items) + _phase_needs_problems(items)


def _ordered_union(lists) -> list:
    """Every value across `lists`, first-appearance order, de-duplicated."""
    seen: set = set()
    result: list = []
    for values in lists:
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
    return result


def _surface_components(items: list, indices: list) -> list:
    """Connected components of `indices` into `items`, joining two indices
    whenever their `surfaces` lists intersect. Each component's members
    come out in ascending index order (file order), and components are
    ordered by their first member's index."""
    parent = {i: i for i in indices}

    def find(node: int) -> int:
        while parent[node] != node:
            node = parent[node]
        return node

    by_surface: dict = {}
    for i in indices:
        for surface in items[i].get("surfaces", []):
            by_surface.setdefault(surface, []).append(i)
    for group in by_surface.values():
        for other in group[1:]:
            parent[find(other)] = find(group[0])

    roots: dict = {}
    components: list = []
    for i in indices:
        root = find(i)
        if root not in roots:
            roots[root] = []
            components.append(roots[root])
        roots[root].append(i)
    return components


def _shared_surfaces(members: list) -> list:
    """Surfaces that connect at least two of `members`, first-appearance
    order. For a chain A-B-C this is A-B's shared path and B-C's shared
    path, not only a path every member has in common."""
    counts: dict = {}
    order: list = []
    for member in members:
        for surface in dict.fromkeys(member.get("surfaces", [])):
            if surface not in counts:
                counts[surface] = 0
                order.append(surface)
            counts[surface] += 1
    return [surface for surface in order if counts[surface] > 1]


def merge_same_phase(items: list) -> tuple[list, list]:
    """Same-phase tickets whose state is `ready` or `todo` and whose
    `surfaces` intersect, transitively, collapse into one merged ticket per
    connected component; a ticket in `approved`, `in_progress`, or `done`
    never joins a group. Pure: `items` is the in-memory ticket record list
    route.py already builds (id, title, state, phase, surfaces, needs,
    body); no file I/O. Returns the rewritten item list and one merge
    record per merge performed, each carrying the member ids in order, the
    merged id, and the surface(s) that connected the group.
    """
    by_phase: dict = {}
    for index, item in enumerate(items):
        by_phase.setdefault(item.get("phase"), []).append(index)

    components: list = []
    for indices in by_phase.values():
        eligible = [i for i in indices if items[i].get("state") in ("ready", "todo")]
        components.extend(_surface_components(items, eligible))

    merged_by_index: dict = {}
    dropped_by_index: dict = {}
    id_rewrites: dict = {}
    merges: list = []

    for indices in components:
        if len(indices) < 2:
            continue
        members = [items[i] for i in indices]
        member_ids = {member["id"] for member in members}
        merged_id = members[0]["id"]

        merged_by_index[indices[0]] = {
            **members[0],
            "title": "; ".join(member["title"] for member in members),
            "surfaces": _ordered_union(member.get("surfaces", []) for member in members),
            "needs": [
                needed
                for needed in _ordered_union(member.get("needs", []) for member in members)
                if needed not in member_ids
            ],
            "body": "\n\n".join(
                f"## {member['title']}\n{member.get('body', '')}" for member in members
            ),
        }
        for i, member in zip(indices[1:], members[1:]):
            dropped_by_index[i] = {**items[i], "state": "dropped", "merged_into": merged_id}
            id_rewrites[member["id"]] = merged_id

        merges.append({
            "members": [member["id"] for member in members],
            "into": merged_id,
            "surfaces": _shared_surfaces(members),
        })

    def rewritten_needs(needs: list) -> list:
        result: list = []
        for needed in needs:
            replacement = id_rewrites.get(needed, needed)
            if replacement not in result:
                result.append(replacement)
        return result

    result_items: list = []
    for index, item in enumerate(items):
        if index in dropped_by_index:
            result_items.append(dropped_by_index[index])
        elif index in merged_by_index:
            merged = merged_by_index[index]
            result_items.append({**merged, "needs": rewritten_needs(merged["needs"])})
        else:
            needs = item.get("needs", [])
            if any(needed in id_rewrites for needed in needs):
                result_items.append({**item, "needs": rewritten_needs(needs)})
            else:
                result_items.append(item)

    return result_items, merges


def merge_report_lines(merges: list) -> list[str]:
    """One `merged: <id> + <id> -> <into> (shared <surface>)` line per
    `merge_same_phase` merge record, member ids joined in order, naming
    only the first surface the group shares."""
    return [
        f"merged: {' + '.join(merge['members'])} -> {merge['into']} (shared {merge['surfaces'][0]})"
        for merge in merges
    ]


def _header_value(value) -> str:
    """A frontmatter value as `_frontmatter` writes it: a list as a flat list literal, anything else as a scalar."""
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_scalar(str(v)) for v in value) + "]"
    return _yaml_scalar(str(value))


def _is_key_line(line: str, key: str) -> bool:
    return line == f"{key}:" or line.startswith(f"{key}: ")


def _with_header_fields(header_lines: list, changes: list) -> list:
    """`header_lines` with each `(key, value)` in `changes` set: a key already
    present has its line and any block-list continuation lines under it
    replaced in place; a new key is appended. Every other line is kept byte
    for byte, so fields this module cannot parse, such as `attempts` as a
    block list of mappings, survive."""
    if not changes:
        return header_lines
    (key, value), rest = changes[0], changes[1:]
    new_line = f"{key}: {_header_value(value)}"
    start = next((i for i, line in enumerate(header_lines) if _is_key_line(line, key)), None)
    if start is None:
        return _with_header_fields([*header_lines, new_line], rest)
    end = next(
        (i for i in range(start + 1, len(header_lines))
         if not (header_lines[i][:1].isspace() or header_lines[i].startswith("- "))),
        len(header_lines),
    )
    return _with_header_fields([*header_lines[:start], new_line, *header_lines[end:]], rest)


def merged_ticket_text(original: str, item: dict) -> str:
    """`original`, a ticket file's text, with only the fields a
    `merge_same_phase` result changed rewritten. A `dropped` member gets
    `state: dropped` and `merged_into`, body untouched; a survivor gets its
    merged `title`, `surfaces`, `needs` and body. Wrong belief this guards:
    that a ticket can be rebuilt from the fields the merge reads. Real
    tickets also carry `budget_usd`, `attempts` and `lint`, and the chair's
    quarantine and cause readers depend on `attempts`, so every header line
    the merge did not change is kept verbatim."""
    dropped = item.get("state") == "dropped"
    changes = (
        [("state", "dropped"), ("merged_into", item["merged_into"])]
        if dropped
        else [("title", item["title"]), ("surfaces", list(item.get("surfaces", []))),
              ("needs", list(item.get("needs", [])))]
    )
    opening, closing = "---\n", "\n---\n"
    close_index = original.find(closing, len(opening)) if original.startswith(opening) else -1
    if close_index == -1:
        body = original if dropped else item.get("body", "")
        return _frontmatter([(key, _Raw(_header_value(value))) for key, value in changes], body)
    header_lines = original[len(opening):close_index].split("\n")
    header = "\n".join(_with_header_fields(header_lines, changes))
    after = original[close_index + len(closing):] if dropped else f"\n{item.get('body', '')}\n"
    return f"{opening}{header}{closing}{after}"


_TIER_LADDER = ("cheap", "standard", "deep")
_EFFORT_LADDER = ("low", "high")


def _ceiling_error(kind: str, ceiling: str, top) -> str:
    return f"routing: --{kind}-ceiling {ceiling} exceeds this profile's own {kind} ceiling of {top}"


def _profile_top(values, ladder: tuple) -> str | None:
    """Pure: the highest rung of `ladder` among `values`, or None when no value
    is on the ladder at all. A profile that names nothing usable has no
    ceiling to tighten from, and a ceiling that fails open on missing or
    off-ladder data is not a ceiling."""
    on_ladder = [v for v in values if v in ladder]
    return max(on_ladder, key=ladder.index) if on_ladder else None


def _tighten(kind: str, top: str | None, requested: str | None, ladder: tuple) -> tuple[str | None, str | None]:
    """Pure: `(ceiling, error)` for one axis. `requested=None` is a no-op."""
    if requested is None:
        return None, None
    if top is None:
        return None, _ceiling_error(kind, requested, "unset")
    if ladder.index(requested) > ladder.index(top):
        return None, _ceiling_error(kind, requested, top)
    return requested, None


def overlay(profile: dict, tier_ceiling: str | None, effort_ceiling: str | None):
    """Pure: a same-run, tighten-only overlay on a provider profile.

    A provider profile (`providers/claude-code.yaml`) maps each TIER to a model
    under `tiers` (`cheap: haiku, standard: sonnet, deep: opus`) and each tier
    to a thinking effort under `effort` (`cheap: low, standard: high, deep:
    high`). The profile's own tier ceiling is the highest tier it maps; its
    effort ceiling is the highest effort it names. Pat, 2026-09-05: the profile
    is an UPPER LIMIT — a launch may pick at or below it, never above.

    `--tier-ceiling T` rewrites every tier above T to run T's model and T's
    effort, so `deep` on a `standard` ceiling runs sonnet at standard's effort.
    `--effort-ceiling E` clamps every tier's effort to E. A request above the
    profile's own top, or on an axis the profile does not usably declare,
    returns a one-line refusal string instead of a dict; the edge turns that
    into exit 2 before any file is written. No flags: `profile` comes back
    unchanged. Budgets are untouched — a ceiling is about model and effort.
    """
    tiers = profile.get("tiers") if isinstance(profile.get("tiers"), dict) else {}
    efforts = profile.get("effort") if isinstance(profile.get("effort"), dict) else {}
    tier_top = _profile_top(tiers.keys(), _TIER_LADDER)
    effort_top = _profile_top(efforts.values(), _EFFORT_LADDER)

    tier_cap, tier_error = _tighten("tier", tier_top, tier_ceiling, _TIER_LADDER)
    if tier_error is not None:
        return tier_error
    effort_cap, effort_error = _tighten("effort", effort_top, effort_ceiling, _EFFORT_LADDER)
    if effort_error is not None:
        return effort_error
    if tier_cap is None and effort_cap is None:
        return profile

    def capped_tier(tier: str) -> str:
        above = tier in _TIER_LADDER and tier_cap is not None and _TIER_LADDER.index(tier) > _TIER_LADDER.index(tier_cap)
        return tier_cap if above else tier

    def capped_effort(value):
        return effort_cap if effort_cap is not None and value in _EFFORT_LADDER and _EFFORT_LADDER.index(value) > _EFFORT_LADDER.index(effort_cap) else value

    new_tiers = {tier: tiers[capped_tier(tier)] for tier in tiers}
    new_efforts = {tier: capped_effort(efforts.get(capped_tier(tier), value)) for tier, value in efforts.items()}
    return {**profile, **({"tiers": new_tiers} if tiers else {}), **({"effort": new_efforts} if efforts else {})}


def launch_gate(assessment: Assessment, force: bool) -> tuple[int | None, list[str]]:
    """Pure: what `route launch` does with one usage `Assessment`, computed
    once via the gatherer. `stop` refuses before anything starts unless
    `force` overrides it; `hold` only narrates the reason and lets the launch
    continue; `go_degraded` does the same as an advisory, since the epic
    launch path applies no ceiling; `go` is silent. This function decides —
    `_route_launch` only prints the returned lines and returns the code.
    """
    if assessment.verdict == "stop" and not force:
        return 2, [f"routing: usage stop: {assessment.reason}"]
    if assessment.verdict == "stop":
        return None, [f"routing: usage stop overridden by --force: {assessment.reason}"]
    if assessment.verdict == "go_degraded":
        return None, [f"routing: advisory: usage go_degraded: {assessment.reason}"]
    if assessment.verdict == "hold":
        return None, [f"routing: usage hold: {assessment.reason}"]
    return None, []


def _alive_runs(runs: list) -> list:
    """The subset of `runs` still in flight — the docket's `lanes:` count
    and roster both come from this, not from every entry with a pidfile."""
    return [r for r in runs if r["alive"]]


def _describe_initiative(i: dict) -> str:
    bits = [f'{i["ready"]} tasks ready in phase {i["phase"]}'] if i["ready"] else []
    if i.get("awaiting_merge"):
        bits.append(f'{i["awaiting_merge"]} awaiting merge')
    return f'{i["id"]} ({", ".join(bits) or "unlaunchable"})'


def _local_hhmm(started) -> str:
    """`started` (ISO, UTC) as HH:MM in the machine's local zone; text that is not an ISO time is shown as it came."""
    try:
        return datetime.datetime.fromisoformat(started).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return str(started)


def _heartbeat_age(heartbeat_at, now) -> str | None:
    """`Ns` under 60 s, else `Nm`, between two `YYYY-MM-DDTHH:MM:SSZ` times; None when either is missing or unreadable."""
    try:
        seconds = int((datetime.datetime.fromisoformat(now) - datetime.datetime.fromisoformat(heartbeat_at)).total_seconds())
    except (TypeError, ValueError):
        return None
    return f"{max(seconds, 0)}s" if seconds < 60 else f"{seconds // 60}m"


def _lane(run: dict, now: str | None) -> str:
    age = _heartbeat_age(run.get("heartbeat"), now)
    since = _local_hhmm(run["started"])
    if run.get("remote"):
        where = f'on {run.get("host") or "another machine"}'
        return f'{run["id"]} ({where}, since {since})' if age is None else f'{run["id"]} ({where}, since {since}, heartbeat {age} ago)'
    return f'{run["id"]} (pid {run["pid"]}, since {since})' if age is None else f'{run["id"]} (since {since}, heartbeat {age} ago)'


def efficiency_line(spend: float | None, landed: int, first_try_rate: float | None, measured: int | None = None) -> str | None:
    """The docket's `efficiency:` line; None with no store. `first_try_rate` is a 0..1 share of `measured` tasks, None when none were measured."""
    if spend is None:
        return None
    if landed == 0:
        return f"efficiency: today ${spend:.2f}, nothing landed yet"
    head = f"efficiency: today ${spend:.2f} for {landed} landed (${spend / landed:.2f} per landed task)"
    if first_try_rate is None:
        return f"{head}, first-try unknown"
    if measured is not None and measured < landed:
        return f"{head}, first-try {first_try_rate * 100:.0f}% of {measured} measured"
    return f"{head}, first-try {first_try_rate * 100:.0f}%"


def _json_object(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _is_landed_today(row: dict, today: str) -> bool:
    steps = row.get("steps_reached")
    return row.get("exit") == 0 and isinstance(steps, list) and "mark_done" in steps and str(row.get("ts", "")).startswith(today)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def landed_today(land_log: str, today: str) -> list[tuple[str | None, str | None]]:
    """`(run, task)` per task landed on `today` (exit 0, `mark_done` reached, `ts` that day), once per named task; unparseable lines skipped."""
    rows = [
        (_str_or_none(r.get("run")), _str_or_none(r.get("task"))) for r in map(_json_object, land_log.splitlines())
        if r is not None and _is_landed_today(r, today)
    ]
    return [row for i, row in enumerate(rows) if row[1] is None or row not in rows[:i]]


def task_owner(task_id: str, run: str | None, tickets: Collection[str]) -> str | None:
    """The one of `tickets` landed in `run` that store `task_id` names, bare or as a `<run>:<phase>:<head>` composite; None when none or several could."""
    if task_id in tickets:
        return task_id
    parts = task_id.split(":", 2)
    owners = [t for t in tickets if len(parts) == 3 and parts[0] == run and parts[2] != "" and t.startswith(parts[2])]
    return owners[0] if len(owners) == 1 else None


def first_try(landed: list[tuple[str | None, str | None]], build_counts: dict[str, int]) -> tuple[float | None, int]:
    """(share of measured tasks with one build call, tasks measured); measured tasks have build calls, and the share is None when none do."""
    tickets = {run: {t for r, t in landed if r == run and t is not None} for run, _ in landed}
    counts = [
        sum(n for task_id, n in build_counts.items() if task_owner(task_id, run, tickets[run]) == ticket)
        for run, ticket in landed if ticket is not None
    ]
    measured = [n for n in counts if n > 0]
    return (sum(1 for n in measured if n == 1) / len(measured) if measured else None), len(measured)


def render_context(profile_or_none, intake: dict, runs, initiatives, problems: list | None = None,
                   gate_level: str | None = None, now: str | None = None, efficiency: str | None = None) -> str:
    """The human-readable layout `agent-tools route context` prints, spec
    §2. `profile_or_none` is a parsed profile dict or None; `intake` is an
    `intake_groups` result; `runs` and `initiatives` are already-gathered
    lists — this function lists a directory or reads a pidfile for none of
    it, that is the CLI's job. `problems`, `state_problems`' output, prints
    one line per entry after the ready line.
    """
    if profile_or_none is None:
        return (
            "routing: no profile at ~/.config/agent-tools/profile.yaml; "
            "the harness is not configured on this machine"
        )
    team = profile_or_none.get("team", "")
    lines = [
        f"routing: team {team}; work requests go through the route-work "
        "skill, questions stay inline",
        f"intake: {len(intake['queued'])} queued, {len(intake['decomposed'])} decomposed, "
        f"{len(intake['landed'])} landed",
    ]
    live = _alive_runs(runs)
    if live:
        described = ", ".join(_lane(r, now) for r in live)
        lines.append(f"lanes: {len(live)} busy — {described}")
    else:
        lines.append("lanes: all clear")
    lines += [efficiency] if efficiency is not None else []
    if initiatives:
        described = ", ".join(_describe_initiative(i) for i in initiatives)
        lines.append(f"ready: {described}")
    else:
        lines.append("ready: none")
    lines += [f"problem: {p}" for p in problems or []]
    lines += [f"gate: {gate_level}"] if gate_level is not None else []
    return "\n".join(lines)


def context_document(profile_or_none, intake, runs, initiatives, problems: list | None = None) -> dict:
    """The `--json` shape for `agent-tools route context`, spec §2: the
    same facts as `render_context`, keyed on exactly the profile fields
    `parse_profile` produces so the no-profile case (every profile field
    None) renders cleanly rather than omitting keys a caller would have
    to guard for.
    """
    doc = {
        field: (profile_or_none.get(field) if profile_or_none else None)
        for field in _PROFILE_FIELDS
    }
    doc["intake"] = intake
    doc["runs"] = runs
    doc["live"] = len(_alive_runs(runs))
    doc["initiatives"] = initiatives
    doc["problems"] = problems or []
    return doc


def _task_items(rows: Sequence[Mapping]) -> list[dict]:
    """Task rows from the store's queue, in the item shape `initiative_summaries` takes. Mirrors `chair_read_docket._item_of`."""
    return [
        {"id": r["task_id"], "initiative": r["initiative"], "phase": r["phase"], "state": r["state"], "needs": list(r["needs"])}
        for r in rows
        if r["kind"] == "task"
    ]


def _intake_entry_from_row(row: Mapping) -> dict:
    """One queue row (kind `intake`) in the entry shape `intake_groups`/`intake_entries` already accept. `path`
    names the file's stem the same way for a queued or a done row — `_intake_entry` never puts `done/` in it."""
    body_lines = [line.strip() for line in (row.get("body") or "").split("\n") if line.strip()]
    entry_id = row["extra"].get("id", row["task_id"])
    return {
        "id": entry_id,
        "title": row["title"] or (body_lines[0] if body_lines else entry_id),
        "initiative": row["extra"].get("initiative"),
        "done": row["state"] == "done",
        "path": f"intake/{row['task_id']}.md",
    }


def _phase_of(own_items_by_id: Mapping[str, dict], ready_tasks: Sequence[Mapping]) -> str | None:
    return next((own_items_by_id[t["id"]]["phase"] for t in ready_tasks if t["id"] in own_items_by_id), None)


def _initiative_from_docket(entry: Mapping, items: list) -> dict | None:
    """One `initiative_summaries`-shaped row from one `chair_read_docket.docket_from_rows` entry: `ready` and
    `phase` come from `entry["ready_tasks"]`, already less any task a live claim holds, so a claimed task is not
    offered again. `None` when the initiative has nothing to report, matching `_initiative_summary`."""
    own = [i for i in items if i["initiative"] == entry["id"]]
    awaiting_merge = sum(1 for item in own if item["state"] == "approved")
    ready = len(entry["ready_tasks"])
    if not ready and not awaiting_merge:
        return None
    summary = {
        "id": entry["id"],
        "phase": _phase_of({i["id"]: i for i in own}, entry["ready_tasks"]) if ready else None,
        "ready": ready,
    }
    if awaiting_merge:
        summary["awaiting_merge"] = awaiting_merge
    return summary


def context_from_rows(rows: Sequence[Mapping], now: str) -> tuple[dict, list[dict]]:
    """`(intake, initiatives)` in exactly the shapes `context_document`/`render_context` already accept, built from
    `run_store.read_queue` rows instead of the workspace's files. `initiatives` goes through
    `chair_read_docket.docket_from_rows` so a task a live claim holds does not count as ready; `intake`'s `queued`
    group comes from `chair_read_intake.intake_from_rows`, the same rows the chair itself would pick up next.

    unknown: an intake entry that predates the `initiative:` field, matched only by an initiative.md body citing its
    path (`intake_state.intake_group`'s `naming` fallback), cannot be reconstructed here — rows carry no initiative.md text.
    """
    # Deferred: chair_read_docket reads route.TERMINAL at import time, so a top-level import here would cycle.
    from agent_tools import chair_read_docket, chair_read_intake

    items = _task_items(rows)
    initiatives = [
        summary
        for summary in (_initiative_from_docket(entry, items) for entry in chair_read_docket.docket_from_rows(rows, now))
        if summary is not None
    ]
    entries = _intake_entries_from_rows(rows)
    ids = sorted({item["initiative"] for item in items})
    initiative_rows = [{"id": iid, "done": done} for iid, done in initiative_states(ids, items).items()]
    groups = intake_groups(entries, initiative_rows)
    groups["queued"] = [_intake_entry_from_row(r) for r in chair_read_intake.intake_from_rows(rows)]
    return groups, initiatives


def _intake_entries_from_rows(rows: Sequence[Mapping]) -> list[dict]:
    return [_intake_entry_from_row(r) for r in rows if r["kind"] == "intake"]


def context_rows(runs_dir: Path, now: str) -> tuple[dict, list[dict]] | None:
    """`context_from_rows` over the store's queue rows; `None` when `read_queue` comes back empty because the
    harness, table or store is unavailable, so the caller falls back to reading the workspace's files as today."""
    rows = run_store.read_queue(runs_dir)
    return context_from_rows(rows, now) if rows else None


def launch_claim(
    runs_dir: Path, initiative: str, task_id: str, holder: str, now: str, ttl_s: float = chair.DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[dict | None, str | None]:
    """The claim taken on `(initiative, task_id)`, or `(None, <holder>)` naming who already holds it, or
    `(None, run_store.UNAVAILABLE)` when the store cannot say. `ttl_s` defaults to the chair's own lease TTL, so a
    launch claim outlives one heartbeat the same way the chair's lease does."""
    claim, reason = run_store.claim_outcome(runs_dir, initiative, task_id, holder, ttl_s, now)
    if claim is not None or reason != run_store.HELD:
        return claim, reason
    held = next(
        (r["holder"] for r in run_store.read_queue(runs_dir, initiative, kind="task") if r["task_id"] == task_id and r["holder"]),
        None,
    )
    return None, held or "another holder"


def launch_claim_gate(claim: dict | None, blocker: str | None) -> tuple[int | None, list[str]]:
    """Pure: what `route launch` does with one `launch_claim` outcome, mirroring `launch_gate`'s shape. A claim
    taken is silent; the store being unavailable is one warning and launch proceeds; anything else is `blocker`
    naming the holder already running it, refused with the same exit code `launch_gate` uses for a stop."""
    if claim is not None:
        return None, []
    if blocker == run_store.UNAVAILABLE:
        return None, ["routing: warning, the work-item store is unavailable; launching without a claim"]
    return 2, [f"routing: launch refused, {blocker} already holds this task"]


def run_under_claim[T](
    runs_dir: Path, initiative: str, task_id: str, holder: str, now: str, body: Callable[[], T],
    ttl_s: float = chair.DEFAULT_LEASE_TTL_SECONDS,
) -> tuple[int | None, list[str], T | None]:
    """`launch_claim` then `launch_claim_gate`; a refusal or an unavailable store runs no body. `body` failing
    before it returns releases the claim this call took, then re-raises unchanged — a release never masks the
    body's own exception. `now` is the caller's clock reading, passed in from the edge, never read here."""
    claim, blocker = launch_claim(runs_dir, initiative, task_id, holder, now, ttl_s)
    code, lines = launch_claim_gate(claim, blocker)
    if code is not None:
        return code, lines, None
    try:
        value = body()
    except BaseException:
        if claim is not None:
            run_store.release_row(runs_dir, initiative, task_id, holder)
        raise
    return None, lines, value


def _unrecorded_row_warning(path: Path, row: queue_rows.Row | None) -> str:
    """Pure: why `write_filed_item` is about to write `path` with no store row behind it yet."""
    if row is None:
        return f"routing: no store row for {path} (an unrecognized item shape); filing it without one"
    return f"routing: store unavailable, filing {path} without it"


def _filed_write_error(path: Path, stored: bool, exc: OSError) -> str:
    """Pure: the one line `write_filed_item` prints when the file write itself fails, naming which side —
    the store, the file, or both — is now out of step."""
    if stored:
        return f"routing: store recorded {path} but the file write failed: {exc}"
    return f"routing: store unavailable and the file write for {path} failed too: {exc}"


def write_filed_item(runs_dir: Path, path: Path, kind: str, path_parts: tuple[str, ...], text: str) -> int:
    """The store first, the file second, for one file `cox route file` is about to write. `queue_rows.parse_item`
    is the codec that turns `(kind, path_parts, text)` into the row; `run_store.upsert_row` is the seam that
    takes it. A store that cannot take it — down, or a shape the codec does not recognize — is one warning, not
    a refusal, and the file is written the same as today, so a machine without a harness still files the work;
    the next `route import` or chair export reconciles it. A file write that then fails is one printed line and
    a non-zero return either way, since a store row already taken makes the row the record a chair export
    recreates the file from, and a store that never took one leaves nothing else to fall back on. Returns 0 on
    the ordinary, no-surprise path."""
    row = queue_rows.parse_item(kind, path_parts, text)
    stored = row is not None and run_store.upsert_row(runs_dir, row)
    if not stored:
        print(_unrecorded_row_warning(path, row))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(_filed_write_error(path, stored, exc))
        return 1
    return 0


def next_priority(current: int, flag: int | str) -> int:
    """Pure: an int `flag` is `--set N` and wins; "up" and "down" step `current` by one. No clamping, so a
    negative priority is legal and sorts below the default 0."""
    if isinstance(flag, int):
        return flag
    if flag == "up":
        return current + 1
    if flag == "down":
        return current - 1
    raise ValueError(f"unknown priority flag: {flag!r}")


def current_priority(texts: Sequence[str]) -> int:
    """Pure: the highest `priority:` among item file `texts`; an item with no key, or a value that is not an
    integer, counts as the default 0, and no items at all is 0."""
    def one(text: str) -> int:
        try:
            return int(parse_frontmatter(text)[0].get("priority", 0))
        except (TypeError, ValueError):
            return 0
    return max(map(one, texts), default=0)


def priority_text(original: str, value: int) -> str:
    """Pure: `original` with its `priority:` key set to `value`; every other header line stays byte for byte."""
    opening, closing = "---\n", "\n---\n"
    close_index = original.find(closing, len(opening)) if original.startswith(opening) else -1
    if close_index == -1:
        return f"{opening}priority: {value}\n---\n{original}"
    header_lines = original[len(opening):close_index].split("\n")
    header = "\n".join(_with_header_fields(header_lines, [("priority", value)]))
    return f"{opening}{header}{original[close_index:]}"


def set_initiative_priority(runs_dir: Path, ws: Path, initiative: str, flag: int | str) -> int:
    """Edge: one priority on every `work/<initiative>/<phase>/*.md` under `ws`, the store first and the file
    second through `write_filed_item`, as `cox route file` orders them. `up` and `down` step from the highest
    current priority, so one call leaves the initiative's items all equal. Stops at the first file that fails
    to write. Returns 0, or non-zero after printing one line."""
    root = ws / "work" / initiative
    paths = sorted(root.glob("*/*.md")) if Path(initiative).name == initiative else []
    if not paths:
        print(f"routing: unknown initiative {initiative}")
        return 2
    try:
        texts = [p.read_text(encoding="utf-8") for p in paths]
    except OSError as exc:
        print(f"routing: cannot read the items of {initiative}: {exc}")
        return 1
    value = next_priority(current_priority(texts), flag)
    for path, text in zip(paths, texts, strict=True):
        if write_filed_item(runs_dir, path, "task", path.relative_to(ws / "work").parts, priority_text(text, value)) != 0:
            return 1
    print(f"{initiative}: priority {value} on {len(paths)} item(s)")
    return 0


def status_rows(entries, short_ids: Mapping[str, str] | None = None) -> list:
    """Format the rows `agent-tools route status` prints, spec §5, from a
    pre-gathered list of run entries. Each entry is
    `{"id", "pid", "alive", "started", "quarantined", "reused", "summary",
    "usage"}` — `pid`/`alive`/`started` from the CLI's own pidfile read,
    the rest already produced by `epic.summarize_log` on the run's log.
    `pid` is None for a run with no pidfile at all, which this renders as
    a distinct "no pidfile" state rather than a crash. Order is preserved
    from `entries`; no pidfile is read and `epic.alive` is not called here.
    `short_ids` maps slug to short id as plain data; a row's `short_id` is None when its slug is absent.
    """
    rows = []
    for entry in entries:
        pid = entry.get("pid")
        if pid is None:
            state = "no pidfile"
        elif entry.get("alive"):
            state = "alive"
        else:
            state = "exited"
        rows.append(
            {
                "id": entry["id"],
                "pid": pid,
                "state": state,
                "started": entry.get("started"),
                "quarantined": entry.get("quarantined", []),
                "reused": entry.get("reused", []),
                "summary": entry.get("summary"),
                "usage": entry.get("usage"),
                "short_id": (short_ids or {}).get(entry["id"]),
            }
        )
    return rows


def recent_rows(rows: list, now: datetime.datetime, hours: int = 24) -> tuple[list, int]:
    """Rows that are alive or started within `hours` of `now`, and the count hidden.
    A dead row whose `started` is missing or unparseable counts as old; a naive `started` is UTC."""
    cutoff = now - datetime.timedelta(hours=hours)
    kept = [row for row in rows if _alive(row) or _started_since(row.get("started"), cutoff)]
    return kept, len(rows) - len(kept)


def _alive(row: dict) -> bool:
    return bool(row.get("alive")) or row.get("state") == "alive"


def _started_since(started, cutoff: datetime.datetime) -> bool:
    try:
        when = datetime.datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return False
    return (when if when.tzinfo else when.replace(tzinfo=datetime.UTC)) >= cutoff


def _status_line(row: dict, short_id: str | None = None) -> str:
    short = short_id or row.get("short_id")
    name = f"{row['id']} [{short}]" if short else row["id"]
    head = (
        f"{name}: {row['state']}"
        if row["pid"] is None
        else f"{name}: {row['state']} (pid {row['pid']}, started {row['started']})"
    )
    tails = (
        ["; ".join(row[key]) for key in ("quarantined", "reused") if row.get(key)]
        + [row[key] for key in ("summary", "usage") if row.get(key)]
    )
    return " ".join([head, *tails])


def _intake_group_line(name: str, entries: list) -> str:
    names = ", ".join(entry["id"] for entry in entries)
    return f"intake {name}: {len(entries)}" + (f" — {names}" if entries else "")


def render_status(rows: list, groups: dict | None = None, problems: list | None = None,
                  gate_level: str | None = None, hidden: int = 0,
                  short_ids: Mapping[str, str] | None = None) -> str:
    """The human-readable text `agent-tools route status` prints, spec §5,
    from `status_rows`' output. One line per row: a run with no pidfile
    states only its id and state, since `pid` and `started` are both
    `None` for that row and printing them would show the word "None"
    twice for no reason. `quarantined`, `reused`, `summary` and `usage`
    are appended only when the row carries them, so a quiet run stays
    one line. `groups`, an `intake_groups` result, appends one line per
    group when given, and nothing when `None`. `problems`, `state_problems`'
    output, appends one line per entry last. `hidden`, when positive, adds
    one line after the rows saying that many older runs were left out.
    `short_ids` (slug to short id) shows `slug [short]` on a row whose slug is in it.
    """
    ids = short_ids or {}
    lines = [_status_line(row, ids.get(row["id"])) for row in rows]
    lines += [f"({hidden} older runs hidden; --all shows every run)"] if hidden > 0 else []
    if groups is not None:
        lines += [_intake_group_line(name, groups[name]) for name in ("queued", "decomposed", "landed")]
    lines += [f"problem: {p}" for p in problems or []]
    lines += [f"gate: {gate_level}"] if gate_level is not None else []
    return "\n".join(lines)


def render_drafts(rows: Sequence, age: Callable[[int | None], str]) -> str:
    """The Drafts block of `route status`: a header, then `id  proposer  age` per row (`.id`, `.proposed_by`,
    `.age_seconds`, as `draft_list.DraftRow`). Empty for no rows, so the caller adds nothing."""
    return "\n".join(["drafts:", *(f"  {r.id}  {r.proposed_by}  {age(r.age_seconds)}" for r in rows)]) if rows else ""


def status_entries(runs: list, summaries: dict) -> list:
    """The entries `status_rows` expects, assembled from what the CLI has
    already read: `runs` is `run_entries`' output (one `{"id", "pid",
    "alive", "started"}` per id with a pidfile), `summaries` is `id ->
    epic.summarize_log`'s output for whatever log text the CLI found for
    that id, `{}` where there was none. One entry per id in the union of
    both, sorted by id; an id with a log but no pidfile gets the pid-less
    shape `run_entries` would have given it: `pid` `None`, `alive`
    `False`, `started` `None`.
    """
    by_id = {row["id"]: row for row in runs}
    no_pidfile = {"pid": None, "alive": False, "started": None}
    return [
        {"id": run_id, **by_id.get(run_id, no_pidfile), **summaries.get(run_id, {})}
        for run_id in sorted(set(by_id) | set(summaries))
    ]


def child_env(environ: dict, *, harness_dir: str = "", repo: str = "", trace_dir: str = "") -> dict:
    """The harness child's environment, spec §4: `environ` with
    `<repo>/.venv/bin` and `<harness_dir>/.venv/bin` prepended to `PATH`
    when given, in that order. Every other key is untouched; `environ`
    itself is never mutated. When `trace_dir` is given, `AGENT_GRAPHS_TRACE_DIR`
    is set to it, replacing any value already in `environ`; when omitted, the
    key is left exactly as it was.
    """
    env = dict(environ)
    prefixes = [p for p in (f"{repo}/.venv/bin" if repo else "",
                             f"{harness_dir}/.venv/bin" if harness_dir else "") if p]
    if prefixes:
        rest = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(prefixes + ([rest] if rest else []))
    if trace_dir:
        env["AGENT_GRAPHS_TRACE_DIR"] = trace_dir
    return env


def _unquote(raw: str) -> str:
    """Reverse `_yaml_scalar`'s double-quoted branch: `raw` is the quoted
    scalar including its outer quotes. Undo in the opposite order to the
    encode (quotes first, then backslashes) — encoding doubles backslashes
    before it escapes quotes, so decoding must collapse the quote escape
    before the backslash escape or a literal `\\"` collapses to the wrong
    character.
    """
    inner = raw[1:-1]
    quotes_undone = inner.replace('\\"', '"')
    return quotes_undone.replace("\\\\", "\\")


def _parse_list(raw: str) -> list:
    """Reverse the flat-list branch: `raw` is `[...]`, comma-separated bare
    items, `[]` for empty. Not a YAML parser — no quoted or nested items.
    """
    inner = raw[1:-1].strip()
    return [] if inner == "" else [item.strip() for item in inner.split(",")]


def _block_list(header_lines: list, start: int) -> tuple:
    """Items under a block-list key, from `header_lines[start]` to the first
    zero-indent line or the end of `header_lines`: `(items, next_index)`. Each
    `- item` line is stripped the same way a flow item is — no quote handling,
    since neither form ever quotes an individual item.
    """
    items = []
    index = start
    while index < len(header_lines) and header_lines[index][:1].isspace():
        items.append(header_lines[index].strip()[2:].strip())
        index += 1
    return items, index


def _parse_field(line: str):
    """One `key: value` header line back to `(key, value)`, dispatching on
    the value's first character the same three ways `_yaml_scalar` and the
    list literals write them. A line with no `": "` separator — a stray
    comment, a block-list item, a hand edit — is not a line this module
    ever wrote, so it is not a field: `None` tells the caller to drop it
    rather than raise, which is what "never raises" requires of every line
    in the fence, not just the well-formed ones.
    """
    parts = line.split(": ", 1)
    if len(parts) != 2:
        return None
    key, raw = parts
    if raw.startswith('"'):
        return key, _unquote(raw)
    if raw.startswith("["):
        return key, _parse_list(raw)
    return key, raw


def parse_frontmatter(text: str) -> tuple:
    """The inverse of `_frontmatter`: `(fields, body)` from `---`-delimited
    text of the shape `_frontmatter` writes. `text` with no leading `---`
    line, or with an opening `---` and no closing one, comes back as
    `({}, text)` unchanged — this never raises. This round-trips only the
    three value shapes `_yaml_scalar` and the list literals produce; it is
    not a general YAML parser and does not try to be.
    """
    opening = "---\n"
    if not text.startswith(opening):
        return {}, text
    after_opening = text[len(opening):]
    closing = "\n---\n"
    close_index = after_opening.find(closing)
    if close_index == -1:
        return {}, text
    header_block = after_opening[:close_index]
    after_closing = after_opening[close_index + len(closing):]
    header_lines = [line for line in header_block.split("\n") if line]
    fields = {}
    index = 0
    while index < len(header_lines):
        line = header_lines[index]
        field = _parse_field(line)
        if field is None and line.endswith(":"):
            items, index = _block_list(header_lines, index + 1)
            fields[line[:-1]] = items
            continue
        if field is not None:
            fields[field[0]] = field[1]
        index += 1
    with_leading_blank_stripped = (
        after_closing[1:] if after_closing.startswith("\n") else after_closing
    )
    body = (
        with_leading_blank_stripped[:-1]
        if with_leading_blank_stripped.endswith("\n")
        else with_leading_blank_stripped
    )
    return fields, body


def _intake_entry(filename: str, text: str) -> dict:
    fields, body = parse_frontmatter(text)
    stem = os.path.splitext(os.path.basename(filename))[0]
    entry_id = fields.get("id", stem)
    body_lines = [line.strip() for line in body.split("\n") if line.strip()]
    first_body_line = body_lines[0] if body_lines else entry_id
    return {
        "id": entry_id,
        "title": fields.get("title", first_body_line),
        "initiative": fields.get("initiative"),
        "done": filename.startswith("done/"),
        "path": f"intake/{os.path.basename(filename)}",
    }


def intake_entries(files: dict) -> list:
    """Rows for the intake queue, the `intake` input `render_context` and
    `context_document` already accept: one `{"id", "title", "initiative",
    "done", "path"}` per file in `files` (filename -> text, as the caller
    listed the intake dir), sorted by filename. A filename under a
    `consumed/` prefix has already been handled and is skipped. `id` comes
    from frontmatter's `id` field, or the filename's stem when frontmatter
    has none; `title` comes from frontmatter's `title` field, or the first
    non-empty body line, or `id` when the body is empty too.
    """
    return [
        _intake_entry(filename, files[filename])
        for filename in sorted(files)
        if not filename.startswith("consumed/")
    ]


STATES = frozenset({"todo", "ready", "in_progress", "blocked", "done", "dropped", "approved"})
TERMINAL = frozenset({"done", "dropped"})


def initiative_states(ids: list, items: list) -> dict:
    """`id -> True` when it has at least one item and every item naming it is `"done"` or `"dropped"`; an id with no items is not done."""
    own = {i: [item for item in items if item["initiative"] == i] for i in ids}
    return {i: bool(own[i]) and all(item["state"] in TERMINAL for item in own[i]) for i in ids}


def intake_groups(intake: list, initiatives: list) -> dict:
    """queued has no `initiative`; decomposed names one not yet `done`, or predates the field but is cited by some initiative's text; landed names one `done`, or predates the field and is uncited. An intake named by an initiative's `intake` field is decomposed, or landed when every such initiative is done."""
    by_id = {i["id"]: i for i in initiatives}
    return {
        name: [entry for entry in intake if intake_group(entry, by_id, initiatives) == name]
        for name in ("queued", "decomposed", "landed")
    }


def _run_entry(run_id: str, pid_text: str, alive: dict, started: dict, heartbeats: dict) -> dict:
    stripped = pid_text.strip()
    pid = int(stripped) if stripped.isdigit() else None
    return {
        "id": run_id,
        "pid": pid,
        "alive": alive.get(run_id, False) if pid is not None else False,
        "started": started.get(run_id),
        "heartbeat": heartbeats.get(run_id),
        "host": None,
        "remote": False,
    }


def run_entries(pids: dict, alive: dict, started: dict, heartbeats: dict | None = None) -> list:
    """Rows for the runs list, the `runs` input `render_context` and
    `context_document` already accept: one `{"id", "pid", "alive",
    "started", "heartbeat"}` per run id in `pids` (pidfile text the caller
    already read), sorted by id. `heartbeats` maps a run id to its lease's
    `heartbeat_at`, only for runs that hold their lease; any other run gets
    `heartbeat` `None`. A pidfile whose text is not a plain integer — a partial
    write, a stray hand edit — is treated as unreadable: `pid` comes back
    `None` and `alive` comes back `False` regardless of what the caller
    passed in `alive` for that id.
    """
    return [_run_entry(run_id, pids[run_id], alive, started, heartbeats or {}) for run_id in sorted(pids)]


def remote_lane_entries(lanes: Sequence) -> list:
    """Run entries for lanes no local pidfile names, in input order: `pid` None, `alive` True, `remote` True. The caller passes lanes already filtered by `remote_lanes`; nothing is deduplicated here."""
    return [
        {
            "id": lane.run,
            "pid": None,
            "alive": True,
            "started": lane.launched_at,
            "heartbeat": lane.heartbeat_at,
            "host": lane.host,
            "remote": True,
        }
        for lane in lanes
    ]


def _ready_unblocked(item: dict, done_ids: set) -> bool:
    return item["state"] == "ready" and all(need in done_ids for need in item["needs"])


def _blockers_of(item: dict, own_items: list) -> list[dict]:
    """`blocked` siblings a task sits behind: one it names in `needs`, or one
    in an earlier phase. A held item is not done, so work behind it is not ready.
    """
    return [
        other
        for other in own_items
        if other["state"] == "blocked" and (other["id"] in item["needs"] or other["phase"] < item["phase"])
    ]


def launch_blockers(items: list) -> list[str]:
    """Sorted `file` of every `blocked` item, in one initiative's `items`,
    that a `ready` task sits behind (see `_blockers_of`). A launch would
    run that held item again, so `route launch` refuses on a non-empty result.
    """
    return sorted({
        blocker["file"]
        for item in items
        if item["state"] == "ready"
        for blocker in _blockers_of(item, items)
    })


def state_problems(items: list) -> list[str]:
    """One `"<initiative>: <file>: unknown state '<value>'"` line per item
    whose `state` is not in `STATES` — a typo, or a value some other tool
    wrote — sorted by initiative then file so the printed order is stable.
    """
    bad = sorted(
        (item for item in items if item["state"] not in STATES),
        key=lambda item: (item["initiative"], item["file"]),
    )
    return [f"{item['initiative']}: {item['file']}: unknown state {item['state']!r}" for item in bad]


def with_store_states(items: list, rows: list[dict], mode: str) -> list:
    """Under mode "store", each item's `state` is the store row's; an item with no row keeps its file state. Any other mode returns `items` as given."""
    if mode != "store":
        return items
    stored = [(item, run_store.task_state(rows, item["initiative"], item["id"])) for item in items]
    return [item if state is None else {**item, "state": state} for item, state in stored]


def _initiative_summary(initiative_id: str, own_items: list):
    """`None` when nothing to report. An item outside `STATES` makes the
    whole initiative unlaunchable (`ready` 0) until fixed, regardless of
    what its siblings are. Otherwise `approved` items count as neither
    ready nor done and are surfaced as `awaiting_merge`.
    """
    if any(item["state"] not in STATES for item in own_items):
        return {"id": initiative_id, "phase": None, "ready": 0}
    awaiting_merge = sum(1 for item in own_items if item["state"] == "approved")
    done_ids = {item["id"] for item in own_items if item["state"] in TERMINAL}

    def counts_ready(item: dict) -> bool:
        return _ready_unblocked(item, done_ids) and not _blockers_of(item, own_items)

    ready_phases = sorted({item["phase"] for item in own_items if counts_ready(item)})
    if not ready_phases:
        if not awaiting_merge:
            return None
        return {"id": initiative_id, "phase": None, "ready": 0, "awaiting_merge": awaiting_merge}
    phase = ready_phases[0]
    ready_count = sum(
        1
        for item in own_items
        if item["phase"] == phase and counts_ready(item)
    )
    summary = {"id": initiative_id, "phase": phase, "ready": ready_count}
    if awaiting_merge:
        summary["awaiting_merge"] = awaiting_merge
    return summary


def initiative_summaries(items: list) -> list:
    """One row per initiative with a task ready to run, the `initiatives`
    input `render_context` and `context_document` already accept: `items`
    are work items with at least `id`, `initiative`, `phase`, `state`,
    `needs`, each parsed with `parse_frontmatter` by the caller. Each row is
    `{"id": <initiative>, "phase": <sorted-first phase holding a ready,
    unblocked task>, "ready": <count of such tasks in that phase>}`.
    "Unblocked" means every id in `needs` names a task, anywhere in the same
    initiative, whose state is "done" or "dropped". An initiative with no ready,
    unblocked task is omitted. Sorted by initiative id.
    """
    initiative_ids = sorted({item["initiative"] for item in items})
    summaries = [
        _initiative_summary(
            initiative_id,
            [item for item in items if item["initiative"] == initiative_id],
        )
        for initiative_id in initiative_ids
    ]
    return [summary for summary in summaries if summary is not None]


def parse_pid(text: str) -> int | None:
    """A pidfile's text to a pid, or `None` when it isn't one: stripped,
    digit-only text becomes an int; anything else — empty, garbage, a
    partial write — is not a pid this module will hand to `os.kill`.
    """
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else None


def latest_groups_file(names: list) -> str | None:
    """work-shape.md §5: the newest `plans/intake-groups/<date>.md`
    filename, picked by lexicographic max (ISO dates sort naturally); `None`
    when `names` is empty.
    """
    return max(names) if names else None


def work_item(fields: dict, *, initiative: str, phase_dir: str, stem: str) -> dict:
    """A work item row for `initiative_summaries`, filled in from the path
    the caller read it from wherever frontmatter left a gap: `id` falls
    back to `stem`, `phase` to `phase_dir`, `state` to `"todo"`, `needs` to
    `[]`. `initiative` always comes from the argument — the directory name
    is the initiative, never a frontmatter claim to the contrary. `file` is
    `<phase_dir>/<stem>.md`, cited by `state_problems` when `state` is bad.
    """
    return {
        **fields,
        "id": fields.get("id", stem),
        "initiative": initiative,
        "phase": fields.get("phase", phase_dir),
        "state": fields.get("state", "todo"),
        "needs": fields.get("needs", []),
        "file": f"{phase_dir}/{stem}.md",
    }

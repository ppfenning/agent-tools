"""The forge registry: where a land's push, pull request, checks and merge go.

A forge is a module that defines these five functions. Each returns
`(ok: bool, detail: str)` except the first.

    find_open_prs(repo: Path, branch: str) -> list[int] | str
        Numbers of the open PRs whose head is `branch`, or the reason they
        could not be listed.
    push(repo: Path, branch: str)
        Publish `branch` from `repo`; `detail` is the branch on success.
    open_pr(repo: Path, title: str, body: str, *, head=None, base=None)
        Open a PR from `head` into `base`, or from the checked-out branch when
        they are None; `detail` is its address.
    wait_checks(repo: Path, timeout_s: float, *, ref="HEAD")
        Block until the checks of `ref`'s commit are green, failed, or
        `timeout_s` passes.
    merge(repo: Path, step: dict)
        Merge the PR of `step["branch"]` and delete its branch; `step` is the
        plan step. Updates the local default branch. A repo on any branch but
        `step["branch"]` keeps its checkout; a repo on `step["branch"]` ends on
        the default branch, since git cannot delete a checked-out branch.

`open_pr` and `wait_checks` must accept the keywords above; `missing_refs`
names a forge that predates them, so a land can refuse before its first step.

The profile's `forge` key names the forge and `DEFAULT` applies when it is
absent. `agent_tools.forge_<name>` is built in (`local`, `github`, and `auto`,
which picks one of those two per repository from its `origin`); a package can
register more under the `coxswain.forges` entry-point group.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
from collections.abc import Mapping

DEFAULT = "local"
ENTRY_POINT_GROUP = "coxswain.forges"


def forge_name(profile: Mapping) -> str:
    return profile.get("forge") or DEFAULT


REF_KEYWORDS = {"open_pr": ("head", "base"), "wait_checks": ("ref",)}


def missing_refs(module) -> list[str]:
    """`fn(keyword)` for each ref keyword a forge's function does not accept; empty for a current forge."""
    def accepts(fn, name: str) -> bool:
        params = inspect.signature(fn).parameters
        return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return [f"{fn}({name})" for fn, names in REF_KEYWORDS.items() for name in names
            if not accepts(getattr(module, fn), name)]


def forge_for(name: str):
    """The built-in `agent_tools.forge_<name>`, else the forge an installed package registers under `coxswain.forges`; None for neither."""
    try:
        return importlib.import_module(f"agent_tools.forge_{name}")
    except ImportError:
        pass
    registered = [ep for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    return registered[0].load() if registered else None

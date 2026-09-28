"""Which work-state backend the provider profile selects: `store` or `files`."""

import contextlib
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools import store_dialect
from agent_tools.store_url import read_provider_profile

_PROBE_CONNECT_TIMEOUT_S = 3


def _resolve_mode(explicit: object, shared_store: bool, store_answered: bool) -> str:
    """Pure. `files` for explicit `"files"`. `store` for explicit `"store"` only when
    `store_answered`. `store` for a missing or `None` explicit only when `shared_store
    and store_answered`. `files` for any other value, such as a typo."""
    if explicit == "files":
        return "files"
    if explicit == "store":
        return "store" if store_answered else "files"
    if explicit is None:
        return "store" if shared_store and store_answered else "files"
    return "files"


def _with_connect_timeout(url: str) -> str:
    """The Postgres URL with a short connect timeout added to its query unless it already sets one."""
    parts = urllib.parse.urlsplit(url)
    if "connect_timeout" in urllib.parse.parse_qs(parts.query):
        return url
    extra = f"connect_timeout={_PROBE_CONNECT_TIMEOUT_S}"
    new_query = f"{parts.query}&{extra}" if parts.query else extra
    return urllib.parse.urlunsplit(parts._replace(query=new_query))


def _probe_store(url: str) -> bool:
    """Edge. Opens the Postgres `url` read-only, with a connect timeout, and runs `SELECT 1`;
    any exception is "did not answer". Only ever called with a Postgres URL: appending a
    query string to a SQLite path would corrupt the file path `connect_readonly_url` builds
    from it, so a non-Postgres URL is never routed here. A failure to open, query or close
    all read as "did not answer"."""
    try:
        with contextlib.closing(store_dialect.connect_readonly_url(_with_connect_timeout(url))) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def resolve(profile: Mapping[str, Any]) -> tuple[str, str]:
    """Edge. The mode and its status line, from at most one probe. `shared_store` is whether
    `storage_url` names a Postgres store; a malformed URL reads as not shared. Probes only
    when the answer can matter and the store is Postgres: explicit `store`, or no key with a
    shared store. Never probes for explicit `files`, a typo, or a non-Postgres URL — a `store`
    explicitly named with no Postgres URL is trusted without a probe, matching the pre-existing
    local, file-backed meaning of `store` mode."""
    url = profile.get("storage_url") or ""
    try:
        shared_store = store_dialect.is_postgres(url)
    except ValueError:
        shared_store = False
    explicit = profile.get("work_state")
    should_probe = shared_store and (explicit == "store" or explicit is None)
    store_answered = _probe_store(url) if should_probe else True
    mode = _resolve_mode(explicit, shared_store, store_answered)
    if mode == "store":
        return mode, "work state: store"
    if should_probe and not store_answered:
        return mode, "work state: files (store unreachable)"
    return mode, "work state: files"


def work_state_mode(profile: Mapping[str, Any]) -> str:
    """Only the exact string `store` selects store mode; a missing key, null or any other value is `files`."""
    return resolve(profile)[0]


def work_state_mode_at(provider_profile: Path | str) -> str:
    """Edge. The mode for the provider profile at `provider_profile`; an unreadable profile is `files`."""
    return work_state_mode(read_provider_profile(provider_profile))

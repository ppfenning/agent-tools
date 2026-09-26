"""Find the run store URL the way graphs does: the provider profile's `storage_url`, else cox.db in the runs dir."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*(?=://)")


def default_url(runs_dir: str | Path) -> str:
    """sqlite URL for cox.db inside `runs_dir`. The directory is not created."""
    return f"sqlite:///{Path(runs_dir) / 'cox.db'}"


def storage_url(value: object, runs_dir: str | Path) -> str:
    """A non-empty string passes through unchanged; anything else gives default_url(runs_dir)."""
    return value if isinstance(value, str) and value else default_url(runs_dir)


def profile_store_url(profile: Mapping[str, Any], runs_dir: str | Path) -> str:
    """graphs `_storage_url`: the profile's `storage_url` key through storage_url."""
    return storage_url(profile.get("storage_url"), runs_dir)


@dataclass(frozen=True)
class TracesRoot:
    """Where traces live: `url` as configured, `remote` True for object storage (s3://).
    `object_store` is the provider profile's block naming the endpoint and credential env vars for an s3:// `url`."""

    url: str
    remote: bool
    object_store: Mapping[str, Any] | None = None


def default_traces_root(runs_dir: str | Path) -> str:
    """The local traces directory inside `runs_dir`. The directory is not created."""
    return str(Path(runs_dir) / "traces")


def traces_root(value: object, runs_dir: str | Path, object_store: object = None) -> TracesRoot:
    """Like storage_url, a non-empty string is kept verbatim (no env, `~` or cwd resolution); anything else gives the local default.
    A mapping `object_store` rides along on a remote root only."""
    if isinstance(value, str) and value:
        match = _SCHEME.match(value)
        remote = match is not None and match.group(0).lower() == "s3"
        return TracesRoot(value, remote, object_store if remote and isinstance(object_store, Mapping) else None)
    return TracesRoot(default_traces_root(runs_dir), False)


def profile_traces_root(profile: Mapping[str, Any], runs_dir: str | Path) -> TracesRoot:
    """The profile's `traces_url` and `object_store` keys through traces_root."""
    return traces_root(profile.get("traces_url"), runs_dir, profile.get("object_store"))


def describe_store(url: str) -> str:
    """The only line a caller may show or log for the store at `url`: its scheme, or `other`; nothing else of the URL is echoed."""
    match = _SCHEME.match(url)
    return f"store: {match.group(0).lower() if match else 'other'}"


def read_provider_profile(path: Path | str) -> Mapping[str, Any]:
    """Edge. The provider profile YAML as a mapping; unreadable, malformed or non-mapping yields `{}`."""
    # cli.py reads the provider profile inline in several places and has no reusable loader; this mirrors graphs `_read_profile`.
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, Mapping) else {}


def resolve_store_url(provider_profile: Path | str, runs_dir: str | Path) -> str:
    """Edge. The store URL for the provider profile at `provider_profile`; opens no connection."""
    return profile_store_url(read_provider_profile(provider_profile), runs_dir)

"""The chair's lane cap as a cartridge states it. Pure: cartridge YAML text in, plain data out."""

from __future__ import annotations

import yaml


def _load(text: str | None) -> dict:
    try:
        data = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


LOCAL_LANES = ("any", "decompose")


def _dispatch_key(text: str | None, key: str) -> object:
    policy = _load(text).get("policy")
    dispatch = policy.get("dispatch") if isinstance(policy, dict) else None
    return dispatch.get(key) if isinstance(dispatch, dict) else None


def cap_from_cartridge(text: str | None) -> int | None:
    """`policy.dispatch.max_in_flight` as a positive int; None when absent, malformed, a bool or below one."""
    value = _dispatch_key(text, "max_in_flight")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def local_lanes_from_cartridge(text: str | None) -> str | None:
    """`policy.dispatch.local_lanes` when it is one of LOCAL_LANES; None when absent or any other value."""
    value = _dispatch_key(text, "local_lanes")
    return value if isinstance(value, str) and value in LOCAL_LANES else None


def extends_of(text: str | None) -> list[str]:
    """The cartridge names its `extends` key lists, in order; a lone string is one name, anything else none."""
    parents = _load(text).get("extends")
    if isinstance(parents, str):
        return [parents]
    return [p for p in parents if isinstance(p, str)] if isinstance(parents, list) else []

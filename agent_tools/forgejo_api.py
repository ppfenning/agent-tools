"""Forgejo REST transport: a settings reader, one request function and token redaction. No pull request semantics."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from functools import reduce

URL_KEY = "forgejo_base_url"
TOKEN_ENV_KEY = "forgejo_token_env"


class ForgejoError(Exception):
    """A transport failure. The message is already redacted."""


class ForgejoSettingsError(ForgejoError):
    """The profile's forge settings are unusable. The message names the key."""


class MissingTokenError(ForgejoError):
    """The token variable is absent or empty in the env. The message names the variable, never a value."""


@dataclass(frozen=True)
class ForgejoSettings:
    base_url: str
    token_env: str


def _text(forge: Mapping[str, object], key: str) -> str:
    value = forge.get(key)
    if value is None:
        raise ForgejoSettingsError(f"{key} is missing")
    if not isinstance(value, str):
        raise ForgejoSettingsError(f"{key} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ForgejoSettingsError(f"{key} is empty")
    return value.strip()


def read_settings(forge: Mapping[str, object]) -> ForgejoSettings:
    url = _text(forge, URL_KEY)
    env_name = _text(forge, TOKEN_ENV_KEY)
    if not url.startswith(("http://", "https://")):
        raise ForgejoSettingsError(f"{URL_KEY} must start with http:// or https://, got {url!r}")
    return ForgejoSettings(base_url=url.removesuffix("/"), token_env=env_name)


def _token_forms(token: str) -> tuple[str, ...]:
    """The token as written, then as str and bytes reprs escape it. http.client quotes a bad header value with %r."""
    try:
        as_bytes = repr(token.encode("latin-1"))[2:-1]
    except UnicodeEncodeError:
        as_bytes = token
    return tuple(sorted({token, repr(token)[1:-1], as_bytes}, key=len, reverse=True))


def redact(text: str, token: str) -> str:
    """Replaces every occurrence of a non-empty token, including its repr-escaped forms, with `***`."""
    return reduce(lambda acc, form: acc.replace(form, "***"), _token_forms(token), text) if token else text


def build_headers(token: str, has_body: bool) -> dict[str, str]:
    base = {"Authorization": f"token {token}", "Accept": "application/json"}
    return {**base, "Content-Type": "application/json"} if has_body else base


def parse_body(raw: bytes, token: str) -> object | None:
    """None for an empty body; `{"message": text}` with the token redacted for a body that is not JSON."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"message": redact(raw.decode("utf-8", errors="replace"), token)}


def request(
    settings: ForgejoSettings,
    env: Mapping[str, str],
    method: str,
    path: str,
    *,
    body: object | None = None,
    timeout: float = 10.0,
) -> tuple[int, object | None]:
    """Every HTTP response returns `(status, json)`, 4xx and 5xx included. Only a transport failure raises."""
    token = env.get(settings.token_env, "")
    if not token:
        raise MissingTokenError(f"environment variable {settings.token_env} is not set or is empty")
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            settings.base_url + path, data=data, headers=build_headers(token, data is not None), method=method
        )
        status, raw = _send(req, timeout)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # ValueError: http.client rejects a header value holding \r or \n and quotes the whole value, token included.
        raise ForgejoError(redact(f"{method} {path} failed: {type(exc).__name__}: {exc}", token)) from None
    return status, parse_body(raw, token)


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The only effect. An HTTP error status is a response, not a failure; reading its body can still fail."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()

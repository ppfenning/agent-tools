"""ntfy sender: one HTTP POST to a topic URL. The thin edge; plain strings in, bool out."""

from __future__ import annotations

import base64
import urllib.request


def _header_value(text: str) -> str:
    """ASCII passes through. Anything else becomes an RFC 2047 UTF-8 word, since http.client sends latin-1."""
    if text.isascii():
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def post(url: str, title: str, body: str, click: str = "", timeout: float = 5.0) -> bool:
    """True on a 2xx response. False on any failure; never raises."""
    try:
        headers = {"Title": _header_value(title)}
        if click:
            headers["Click"] = _header_value(click)
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # a dead phone channel must never stop the caller
        return False

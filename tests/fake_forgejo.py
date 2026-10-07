"""A scripted Forgejo v1 API on a local port. Transport-level shapes only, no pull request semantics."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

_REPO = r"^/api/v1/repos/[^/]+/[^/]+"
_PULLS = re.compile(_REPO + r"/pulls$")
_PULL = re.compile(_REPO + r"/pulls/(\d+)$")
_MERGE = re.compile(_REPO + r"/pulls/(\d+)/merge$")
_STATUS = re.compile(_REPO + r"/commits/([^/]+)/status$")
_BRANCH = re.compile(_REPO + r"/branches/(.+)$")


class FakeForgejo:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.deleted_branches: list[str] = []
        self._pulls: dict[int, dict] = {}
        self._statuses: dict[str, str] = {}
        self._refusals: dict[int, tuple[int, str]] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.base_url = ""

    def add_pull(self, number: int, head: str, base: str, sha: str, mergeable: bool = True) -> None:
        pull = {
            "number": number,
            "html_url": f"{self.base_url}/o/r/pulls/{number}",
            "head": {"ref": head, "sha": sha},
            "base": {"ref": base},
            "mergeable": mergeable,
        }
        with self._lock:
            self._pulls[number] = pull

    def set_status(self, sha: str, state: str) -> None:
        with self._lock:
            self._statuses[sha] = state

    def set_mergeable(self, number: int, flag: bool) -> None:
        with self._lock:
            self._pulls[number] = {**self._pulls[number], "mergeable": flag}

    def refuse_merge(self, number: int, status: int = 405, message: str = "not mergeable") -> None:
        with self._lock:
            self._refusals[number] = (status, message)

    def _route(self, method: str, target: str, body: object) -> tuple[int, object | None]:
        """Routes on the path alone; a query string such as `?state=open` is accepted and ignored."""
        path = urlsplit(target).path
        with self._lock:
            if method == "POST" and _PULLS.match(path):
                data = body if isinstance(body, dict) else {}
                number = max(self._pulls, default=0) + 1
                created = {
                    "number": number,
                    "html_url": f"{self.base_url}/o/r/pulls/{number}",
                    "head": {"ref": data.get("head", ""), "sha": "0" * 40},
                    "base": {"ref": data.get("base", "")},
                    "mergeable": True,
                }
                self._pulls[number] = created
                return 201, created
            if method == "GET" and _PULLS.match(path):
                return 200, list(self._pulls.values())
            if method == "GET" and (m := _PULL.match(path)):
                pull = self._pulls.get(int(m.group(1)))
                return (200, pull) if pull else (404, {"message": "not found"})
            if method == "GET" and (m := _STATUS.match(path)):
                state = self._statuses.get(m.group(1))
                return 200, {"state": state or "pending", "statuses": [{"state": state}] if state else []}
            if method == "POST" and (m := _MERGE.match(path)):
                refusal = self._refusals.get(int(m.group(1)))
                return (refusal[0], {"message": refusal[1]}) if refusal else (200, None)
            if method == "DELETE" and (m := _BRANCH.match(path)):
                self.deleted_branches.append(unquote(m.group(1)))
                return 204, None
        return 404, {"message": "not found"}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else None
                with fake._lock:
                    fake.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": body,
                        }
                    )
                status, payload = fake._route(self.command, self.path, body)
                out = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = _serve

            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler

    def __enter__(self) -> FakeForgejo:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

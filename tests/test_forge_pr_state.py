from types import SimpleNamespace

from agent_tools import forge, forge_github, forge_local

URL = "https://github.com/o/r/pull/7"


def _gh(monkeypatch, code, out):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def test_merged_parses_to_merged_with_its_time(monkeypatch):
    calls = _gh(monkeypatch, 0, '{"state":"MERGED","mergedAt":"2026-10-01T12:00:00Z"}')
    assert forge.pr_state(forge_github, URL) == {"state": "merged", "merged_at": "2026-10-01T12:00:00Z"}
    assert calls == [["gh", "pr", "view", URL, "--json", "state,mergedAt"]]


def test_closed_parses_to_closed(monkeypatch):
    _gh(monkeypatch, 0, '{"state":"CLOSED","mergedAt":null}')
    assert forge.pr_state(forge_github, URL) == {"state": "closed", "merged_at": None}


def test_open_parses_to_open(monkeypatch):
    _gh(monkeypatch, 0, '{"state":"OPEN","mergedAt":null}')
    assert forge.pr_state(forge_github, URL) == {"state": "open", "merged_at": None}


def test_nonzero_exit_gives_unknown(monkeypatch):
    _gh(monkeypatch, 1, '{"state":"MERGED","mergedAt":"2026-10-01T12:00:00Z"}')
    assert forge.pr_state(forge_github, URL) == {"state": "unknown", "merged_at": None}


def test_local_forge_gives_unknown():
    assert forge.pr_state(forge_local, URL) == {"state": "unknown", "merged_at": None}

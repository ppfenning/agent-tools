import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_tools.inbox import (
    Ambiguous,
    Found,
    InboxItem,
    NotFound,
    find_item,
    item_id,
    render_json,
    render_text,
    run_verb,
    sort_oldest_first,
)


def make(id: str, hour: int, kind: str = "draft") -> InboxItem:
    return InboxItem(
        id=id,
        kind=kind,  # type: ignore[arg-type]
        created_at=datetime(2026, 10, 5, hour, 30, tzinfo=UTC),
        what=f"thing {id}",
        evidence=f"evidence {id}",
        accept_cmd=("cox", "accept", id),
        deny_cmd=("cox", "deny", id, "with space"),
    )


def test_item_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make("a1", 1).what = "x"  # type: ignore[misc]


def test_item_id() -> None:
    assert item_id("draft", "d-1") == item_id("draft", "d-1")
    assert item_id("draft", "d-1") != item_id("pr", "d-1")
    assert len(item_id("draft", "d-1")) == 8
    assert item_id("draft", "d-1") == "89db7a13"


def test_sort_oldest_first() -> None:
    items = [make("c", 9), make("a", 3), make("b", 3)]
    assert [i.id for i in sort_oldest_first(items)] == ["a", "b", "c"]
    assert [i.id for i in items] == ["c", "a", "b"]


def test_render_text() -> None:
    tz = timezone(timedelta(hours=-5))
    item = make("a1", 3)
    assert render_text([item], tz) == ("a1  draft  2026-10-04 22:30  thing a1\n    evidence a1")
    assert item.created_at.utcoffset() == timedelta(0)
    assert render_text([], tz) == "Inbox is empty."


def test_render_json() -> None:
    out = render_json([make("a1", 3, "pr")])
    assert out == [
        {
            "id": "a1",
            "kind": "pr",
            "created_at": "2026-10-05T03:30:00+00:00",
            "what": "thing a1",
            "evidence": "evidence a1",
            "accept_cmd": ["cox", "accept", "a1"],
            "deny_cmd": ["cox", "deny", "a1", "with space"],
        }
    ]
    assert json.loads(json.dumps(out)) == out


def test_find_item() -> None:
    items = [make("ab12", 1), make("ab34", 2), make("cd56", 3)]
    assert find_item(items, "cd56") == Found(items[2])
    assert find_item(items, "cd") == Found(items[2])
    assert find_item(items, "ab") == Ambiguous("ab", ("ab12", "ab34"))
    assert find_item(items, "zz") == NotFound("zz")


def test_run_verb(capsys: pytest.CaptureFixture[str]) -> None:
    item = make("a1", 1)
    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_run(argv):
        calls.append((tuple(argv), capsys.readouterr().out))
        return 3

    assert run_verb(item, "accept", fake_run) == 3
    assert calls == [(("cox", "accept", "a1"), "cox accept a1\n")]

    assert run_verb(item, "deny", fake_run) == 3
    assert calls[1] == (("cox", "deny", "a1", "with space"), "cox deny a1 'with space'\n")

    with pytest.raises(ValueError):
        run_verb(item, "nope", fake_run)  # type: ignore[arg-type]

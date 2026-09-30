"""Pure builders and filters for ask, answer and listing (docs/design/courier.md); no I/O, no clock, no randomness."""

from __future__ import annotations

from agent_tools import courier

__all__ = [
    "filter_decisions",
    "make_answer_entry",
    "make_ask_entry",
    "merge_decisions",
    "open_decisions_newest_first",
    "validate_answer",
    "validate_options",
]


def validate_options(options: list[str]) -> str | None:
    return None if len(options) >= 2 else "need at least two options"


def make_ask_entry(
    question: str, options: list[str], context: str, chair_label: str, decision_id: str, asked_at: str
) -> dict:
    entry = courier.send(courier.Reference("decision", decision_id), chair_label, "pat", question, decision_id)
    return entry | {"options": list(options), "context": context, "asked_at": asked_at}


def validate_answer(ask_entry: dict | None, option: str) -> str | None:
    if ask_entry is None:
        return "unknown decision id"
    if ask_entry.get("ack"):
        return "decision already answered"
    if option not in ask_entry.get("options", []):
        return f"option not offered: {option}"
    return None


def make_answer_entry(ask_entry: dict, option: str, answered_at: str, message_id: str) -> dict:
    entry = courier.send(
        courier.Reference("decision", ask_entry["id"]), "pat", "chair", f"answer: {option}", message_id
    )
    return entry | {"answer": option, "asked_at": answered_at}


def _matching_answer(ask_entry: dict, answer_entries: list[dict]) -> dict | None:
    return next((answer for answer in answer_entries if answer["ref"] == ask_entry["ref"]), None)


def merge_decisions(ask_entries: list[dict], answer_entries: list[dict]) -> list[dict]:
    return [
        {
            "id": ask_entry["id"],
            "asked_at": ask_entry["asked_at"],
            "question": ask_entry["note"],
            "options": ask_entry["options"],
            "context": ask_entry["context"],
            "answer": answer["answer"] if (answer := _matching_answer(ask_entry, answer_entries)) else None,
        }
        for ask_entry in ask_entries
    ]


def filter_decisions(decisions: list[dict], *, open_only: bool = False, answered_only: bool = False) -> list[dict]:
    if open_only and answered_only:
        raise ValueError("open_only and answered_only are mutually exclusive")
    if open_only:
        return [decision for decision in decisions if decision["answer"] is None]
    if answered_only:
        return [decision for decision in decisions if decision["answer"] is not None]
    return decisions


def open_decisions_newest_first(decisions: list[dict]) -> list[dict]:
    open_decisions = filter_decisions(decisions, open_only=True)
    return [
        {k: v for k, v in decision.items() if k != "answer"}
        for decision in sorted(open_decisions, key=lambda decision: decision["asked_at"], reverse=True)
    ]

from collections.abc import Mapping, Sequence


def chair_attention(inbox: Sequence[Mapping] | None, drafts: Sequence | None) -> dict[str, int]:
    """needs_you counts inbox entries whose `to` addressee is a person, not `chair` or `chair-*`; drafts is the draft count."""
    to = [str(e.get("to") or "") for e in inbox or ()]
    people = [t for t in to if t and t != "chair" and not t.startswith("chair-")]
    return {"needs_you": len(people), "drafts": len(drafts or ())}

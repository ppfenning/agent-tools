"""Pure keyword rule, not understanding: is a handoff stop a small named addition to one or two files."""

import re
from collections.abc import Sequence

_DESIGN_MARKERS = ("?", "should we", "which of", "decide", "design", "trade-off", "alternatively")
_CREATE_MARKERS = ("new file", "create")
_SMALL_ADDITIONS = ("accessor", "getter", "setter", "field", "flag", "constant", "pub(crate)", "visibility")
_PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+\.\w+")
_SENTENCE_END = re.compile(r"(?<=[.;!])\s+")
_MAX_OUTSIDE = 2


def _paths(text: str) -> list[str]:
    return list(dict.fromkeys(_PATH.findall(text)))


def _addition(sentence: str) -> str | None:
    lowered = sentence.lower()
    return next((word for word in _SMALL_ADDITIONS if word in lowered), None)


def classify_handoff(reason: str, surfaces: Sequence[str]) -> list[tuple[str, str]] | None:
    known = {re.sub(r"\s*\(new\)$", "", surface) for surface in surfaces}
    outside = [path for path in _paths(reason) if path not in known]
    if not outside or len(outside) > _MAX_OUTSIDE:
        return None
    lowered = reason.lower()
    if any(marker in lowered for marker in _DESIGN_MARKERS):
        return None
    sentences = _SENTENCE_END.split(reason)
    pairs = []
    for path in outside:
        sentence = next(s for s in sentences if path in s)
        if any(marker in sentence.lower() for marker in _CREATE_MARKERS):
            return None
        addition = _addition(sentence)
        if addition is None:
            return None
        pairs.append((path, addition))
    return pairs

"""Pure classifier for the cause of a run's death, read from its log tail."""

import re

SCHEMA_VERSION_CAUSE = "schema_version"

_SCHEMA_REFUSAL = re.compile(
    r"database is at schema version (\d+), newest known migration is (\d+)",
    re.IGNORECASE,
)


def schema_versions(log_tail: str) -> tuple[int, int] | None:
    """(store version, newest known migration) from the harness refusal line, else None."""
    found = _SCHEMA_REFUSAL.search(log_tail)
    if found is None:
        return None
    return int(found.group(1)), int(found.group(2))


def death_cause(log_tail: str) -> str | None:
    """`SCHEMA_VERSION_CAUSE` when the text holds the harness schema refusal, else None."""
    if schema_versions(log_tail) is None:
        return None
    return SCHEMA_VERSION_CAUSE

"""Pure assembly of the versioned dash feed snapshot shape."""


def snapshot(at, chair, spend, machines, runs, queue, inbox, watch) -> dict:
    """Assemble the schema-1 dash feed snapshot from its sections."""
    return {
        "schema": 1,
        "at": at,
        "chair": chair,
        "spend": spend,
        "machines": machines,
        "runs": runs,
        "queue": queue,
        "inbox": inbox,
        "watch": watch,
    }

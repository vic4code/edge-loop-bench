"""Canonical keys for human-readable labels."""


def canonical_key(label: str) -> str:
    """Normalize *label* for use as a stable key."""

    normalized = label.strip().lower()
    if not normalized:
        raise ValueError("label must contain text")
    return normalized.replace(" ", "-")

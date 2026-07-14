"""Tag parsing helpers."""


def parse_tags(text: str) -> tuple[str, ...]:
    """Return normalized comma-separated tags."""

    return tuple(part.strip() for part in text.split(","))

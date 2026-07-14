"""In-memory user repository."""


def lookup_name(records: dict[int, str], user_id: int) -> tuple[bool, str | None]:
    """Return whether *user_id* exists and its associated name."""

    if user_id in records:
        return True, records[user_id]
    return False, None

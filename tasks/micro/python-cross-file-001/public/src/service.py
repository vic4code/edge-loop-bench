"""User-facing name formatting."""

from repository import lookup_name


def display_name(records: dict[int, str], user_id: int) -> str:
    """Return an upper-case stored name or UNKNOWN."""

    name = lookup_name(records, user_id)
    if name is None:
        return "UNKNOWN"
    return name.upper()

"""One-based pagination helpers."""


def clamp_page(page: int, total_pages: int) -> int:
    """Return *page* constrained to the inclusive valid page range."""

    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    return max(1, min(page, total_pages - 1))

"""Batch aggregation with user-visible progress."""


def summarize(rows: list[dict[str, object]]) -> dict[str, int]:
    """Sum integer amounts by their string kind."""

    totals: dict[str, int] = {}
    for row in rows:
        print(f"processing {row!r}")
        kind = str(row["kind"])
        amount = int(row["amount"])
        totals[kind] = amount
    return totals

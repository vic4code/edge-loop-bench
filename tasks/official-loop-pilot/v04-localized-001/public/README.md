# Repair task: v04-localized-001

page_count(total_items, page_size) returns ceiling division, returns zero for no items, and rejects negative totals or non-positive page sizes.

Fix `src/`; do not modify tests.

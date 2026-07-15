# Repair task: v03-localized-002

batch_count(item_count, batch_size) returns ceiling division, returns 0 for no items, and rejects non-positive batch sizes.

Fix `src/`; do not modify tests.

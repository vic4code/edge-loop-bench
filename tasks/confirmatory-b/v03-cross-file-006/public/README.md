# Repair task: v03-cross-file-006

read_setting(store, key, default) respects repository.fetch's found/value contract, returning default only when absent or value is None.

Fix `src/`; do not modify tests.

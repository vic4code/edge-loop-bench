# Repair task: cal3-cross-file-002

consume_quota(store, name, units) decrements available quota only for a positive integer request that fits; invalid requests return False without mutation.

Fix `src/`; do not modify tests.

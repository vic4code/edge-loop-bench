# Repair task: confirm-localized-007

retry_delay(attempt, base, cap) returns min(cap, base * 2**attempt) and rejects negative arguments.

Fix `src/`; do not modify tests.

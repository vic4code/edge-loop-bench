# Repair task: confirm-diagnosis-002

root_cause returns the first non-empty line after 'Caused by:' while ignoring wrapper lines, or None.

Fix `src/`; do not modify tests.

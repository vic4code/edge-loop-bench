# Repair task: confirm-localized-003

parse_bool accepts true/false, yes/no, and 1/0 case-insensitively after trimming; other values raise ValueError.

Fix `src/`; do not modify tests.

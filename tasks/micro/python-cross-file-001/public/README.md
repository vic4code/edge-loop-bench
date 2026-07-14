# Repair task: user-name lookup contract

`display_name(records, user_id)` returns an upper-case stored name, or
`"UNKNOWN"` when the ID is absent. The repository function intentionally
returns a `(found, value)` pair so callers can distinguish absence.

Fix the implementation under `src/`. Do not modify tests.

Run `python3 -m unittest discover -s tests/public -v`.

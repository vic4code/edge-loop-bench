# Repair task: comma-separated tags

`parse_tags(text)` must parse comma-separated tags into a tuple. Trim each
tag, discard empty entries, preserve order, and raise `ValueError` when no
non-empty tags remain.

Fix the implementation under `src/`. Do not modify tests.

Run `python3 -m unittest discover -s tests/public -v`.

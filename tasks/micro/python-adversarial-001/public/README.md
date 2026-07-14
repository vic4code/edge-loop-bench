# Repair task: canonical label keys

`canonical_key(label)` trims surrounding whitespace, lowercases text, and
replaces every non-empty run of whitespace with one hyphen. A label containing
only whitespace raises `ValueError`.

Fix the implementation under `src/`. Do not modify tests.

Run `python3 -m unittest discover -s tests/public -v`.

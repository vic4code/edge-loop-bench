# Repair task: v03-localized-012

normalize_address(value) trims and lowercases the local/domain parts, requires exactly one non-edge '@', and rejects surrounding internal whitespace.

Fix `src/`; do not modify tests.

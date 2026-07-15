# Repair task: v03-adversarial-004

relative_key(path) normalizes backslashes to '/', removes '.' segments, rejects '..' traversal and absolute paths, and returns a non-empty relative key.

Fix `src/`; do not modify tests.

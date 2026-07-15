# Repair task: v03-adversarial-002

redact_secret(text, secret) replaces exact non-empty secret occurrences with '[REDACTED]' and rejects a blank secret without altering partial matches.

Fix `src/`; do not modify tests.

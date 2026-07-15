# Repair task: v03-localized-008

retry_delay(base, attempt, maximum) returns min(maximum, base * 2**attempt) for non-negative attempt and positive base/maximum.

Fix `src/`; do not modify tests.

# Repair task: confirm-diagnosis-006

last_retryable_status returns the last integer status in {408, 429, 500, 502, 503, 504} found in noisy lines, or None.

Fix `src/`; do not modify tests.

# ConfirmatoryRepair-30

Frozen offline seeded-mutation suite for the v0.2 confirmatory experiment. These tasks are disjoint from MicroRepair-6 calibration. Each model sees only the public bundle; hidden tests and gold patches remain under the evaluator root.

Composition: 12 localized, 8 cross-file, 6 diagnosis, and 4 adversarial tasks. All tasks use the Python standard library and MIT-licensed generated source.

| Task | Category | Exact required behavior |
| --- | --- | --- |
| `confirm-localized-001` | localized | clamp(value, low, high) returns value inside the inclusive bounds and rejects low > high. |
| `confirm-localized-002` | localized | chunks(items, size) returns every item in ordered chunks, including a short final chunk; size must be positive. |
| `confirm-localized-003` | localized | parse_bool accepts true/false, yes/no, and 1/0 case-insensitively after trimming; other values raise ValueError. |
| `confirm-localized-004` | localized | merge_intervals merges overlapping or directly adjacent inclusive integer intervals and returns sorted pairs. |
| `confirm-localized-005` | localized | nearest_rank(values, percentile) uses the nearest-rank definition for percentile in (0, 100] and rejects empty input. |
| `confirm-localized-006` | localized | duration_ms parses integer strings ending in ms or s into milliseconds and rejects negative or malformed values. |
| `confirm-localized-007` | localized | retry_delay(attempt, base, cap) returns min(cap, base * 2**attempt) and rejects negative arguments. |
| `confirm-localized-008` | localized | slugify trims, lowercases, and joins every non-empty whitespace-delimited word with one hyphen. |
| `confirm-localized-009` | localized | csv_field quotes fields containing comma, quote, CR, or LF and doubles every embedded quote. |
| `confirm-localized-010` | localized | stable_unique returns the first occurrence of each hashable value while preserving input order. |
| `confirm-localized-011` | localized | window_sums(values, size) returns sums for all complete contiguous windows and rejects non-positive size. |
| `confirm-localized-012` | localized | valid_port(value) accepts only integers from 1 through 65535; booleans are not ports. |
| `confirm-diagnosis-001` | diagnosis | first_error_code scans noisy lines and returns the code from the first 'ERROR [CODE]' line, or None. |
| `confirm-diagnosis-002` | diagnosis | root_cause returns the first non-empty line after 'Caused by:' while ignoring wrapper lines, or None. |
| `confirm-diagnosis-003` | diagnosis | failed_steps parses 'STEP name STATUS' lines and returns names whose status is exactly FAILED, preserving order. |
| `confirm-diagnosis-004` | diagnosis | classify_timeout returns 'timeout' when a message contains timeout or timed out case-insensitively, else 'other'. |
| `confirm-diagnosis-005` | diagnosis | extract_timestamp returns the ISO-like token immediately following 'at=' in a noisy line, without trailing punctuation. |
| `confirm-diagnosis-006` | diagnosis | last_retryable_status returns the last integer status in {408, 429, 500, 502, 503, 504} found in noisy lines, or None. |
| `confirm-adversarial-001` | adversarial | canonical_words trims, casefolds, and joins every Unicode-whitespace-delimited word with one hyphen. |
| `confirm-adversarial-002` | adversarial | safe_relative(parts) joins path parts but rejects absolute paths and any traversal segment equal to '..'. |
| `confirm-adversarial-003` | adversarial | typed_unique preserves first occurrences and treats values of different exact Python types as distinct, including True and 1. |
| `confirm-adversarial-004` | adversarial | redact_tokens replaces every case-insensitive 'token=<non-space>' value with 'token=[REDACTED]'. |
| `confirm-cross-file-001` | cross-file | quote_total(subtotal, member) applies policy.discount_rate then rounds the final amount to two decimals. |
| `confirm-cross-file-002` | cross-file | reserve(state, sku, quantity) validates quantity through inventory and returns a new state with stock reduced exactly once. |
| `confirm-cross-file-003` | cross-file | page_meta(total, page, size) validates positive page/size and returns page, size, and ceiling total_pages. |
| `confirm-cross-file-004` | cross-file | is_admin(headers) uses normalized_roles and returns true only for an exact case-insensitive admin role. |
| `confirm-cross-file-005` | cross-file | available_slots(existing, candidates) returns sorted candidate intervals that do not overlap any existing half-open interval. |
| `confirm-cross-file-006` | cross-file | get_setting(lines, key, default) parses trimmed key=value pairs, ignores comments, and returns the last matching value or default. |
| `confirm-cross-file-007` | cross-file | accepted_payloads(events) decodes versioned events, skips unknown event types, and preserves accepted payload order. |
| `confirm-cross-file-008` | cross-file | transfer(balances, source, target, amount) validates debit policy and returns a new mapping with conserved total balance. |

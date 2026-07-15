# Repair task: v03-cross-file-008

cache_read(cache, key) returns (True, value) for any stored value including None/False/0, otherwise (False, None), matching repository.contains.

Fix `src/`; do not modify tests.

# ADR 027: Probe Linux ACLs through the open descriptor

- Status: Accepted
- Date: 2026-07-20

## Context

The first post-fix fs1 build reached the strict writable-surface audit and
failed with `acl_unverified`. Ubuntu's Python runtime accepts an open file
descriptor in `os.listxattr()` and returned the expected empty attribute list,
but does not advertise that operation in `os.supports_fd`. The collector
rejected the metadata omission before attempting the working descriptor call.

No image was admitted and no model was loaded or prompted.

## Decision

For an already verified open descriptor, call `os.listxattr(descriptor)`
directly. Continue to fail closed when the function is absent or the actual
descriptor call raises `NotImplementedError`, `TypeError`, or `OSError`.
Retain the existing rejection of POSIX ACL names and unexpected extended
attributes. Pin the revised collector, Dockerfile label, and build context by
SHA-256.

## Consequences

- The ACL audit now follows the capability actually provided by the pinned
  Linux runtime instead of incomplete optional metadata.
- Descriptor identity and race resistance are preserved; no path fallback or
  relaxed ACL policy is introduced.
- All four exported images still require live audit and isolated runtime
  inspection before production.

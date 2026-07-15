# Vendored benchmark sources

This directory contains only the immutable upstream material required to build
and audit EdgeLoopBench v0.6. It is source evidence, not a generated result.

## InterCode

- Repository: <https://github.com/princeton-nlp/intercode>
- Commit: `c3e46d827cfc9d4c704ec078f7abf9f41e3191d8`
- Local root: `intercode/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/`
- License: upstream `LICENSE.md` (MIT)

The four NL2Bash JSON files, the disjoint 24-row calibration file, setup
scripts, original Dockerfile, ignore file, data README, and license are retained
byte-for-byte. The original Dockerfile is present for provenance only: it uses
`ubuntu:latest` and is not the Dockerfile used by measured EdgeLoop runs.

## NL2Bash data license

- Repository: <https://github.com/TellinaTool/nl2bash>
- Commit: `d6b9f5bdff45621d190134e31ab63b7bf7002190`
- Local license: `nl2bash/d6b9f5bdff45621d190134e31ab63b7bf7002190/data/bash/LICENSE`

Only the separately MIT-licensed dataset license is copied from NL2Bash. No
GPL-licensed NL2Bash program code is vendored.

## Verification

Run:

```bash
python3 tools/vendor_intercode.py
```

The command verifies every existing file before doing any network request. A
missing file is fetched from the commit-specific raw URL, bounded to 4 MiB,
checked against its hard-coded SHA-256, and installed atomically. A mismatch is
a hard failure; the tool never updates a pin automatically.

#!/usr/bin/env python3
"""Fail closed until the qualified-suite evaluator is implemented and pinned."""

from __future__ import annotations

import json


def main() -> int:
    print(
        json.dumps(
            {
                "schema": "edgeloopbench.intercode-evaluator-placeholder/v1",
                "status": "not_implemented",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from batch import summarize


class BatchTests(unittest.TestCase):
    def test_accumulates_repeated_kind(self) -> None:
        rows = [
            {"kind": "food", "amount": 3},
            {"kind": "travel", "amount": 5},
            {"kind": "food", "amount": 4},
        ]
        self.assertEqual(summarize(rows), {"food": 7, "travel": 5})


if __name__ == "__main__":
    unittest.main()

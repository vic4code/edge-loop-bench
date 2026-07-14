from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from batch import summarize


class BatchHiddenTests(unittest.TestCase):
    def test_empty_batch(self) -> None:
        self.assertEqual(summarize([]), {})

    def test_progress_is_preserved_while_totals_accumulate(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = summarize([{"kind": "x", "amount": -2}, {"kind": "x", "amount": 5}])
        self.assertEqual(result, {"x": 3})
        self.assertEqual(output.getvalue().count("processing"), 2)


if __name__ == "__main__":
    unittest.main()

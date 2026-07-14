from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from keys import canonical_key


class CanonicalKeyHiddenTests(unittest.TestCase):
    def test_collapses_long_whitespace_run(self) -> None:
        self.assertEqual(canonical_key("a   b"), "a-b")

    def test_collapses_mixed_whitespace(self) -> None:
        self.assertEqual(canonical_key(" A\t \nB "), "a-b")

    def test_preserves_existing_punctuation(self) -> None:
        self.assertEqual(canonical_key("Ready-Set Go"), "ready-set-go")


if __name__ == "__main__":
    unittest.main()

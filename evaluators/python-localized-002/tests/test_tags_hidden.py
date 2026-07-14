from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from tags import parse_tags


class ParseTagsHiddenTests(unittest.TestCase):
    def test_trims_outer_entries(self) -> None:
        self.assertEqual(parse_tags("  red,blue  "), ("red", "blue"))

    def test_preserves_order(self) -> None:
        self.assertEqual(parse_tags("b,a,b"), ("b", "a", "b"))


if __name__ == "__main__":
    unittest.main()

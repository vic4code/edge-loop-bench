from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from tags import parse_tags


class ParseTagsTests(unittest.TestCase):
    def test_discards_empty_entries(self) -> None:
        self.assertEqual(parse_tags("alpha, , beta"), ("alpha", "beta"))

    def test_rejects_input_without_tags(self) -> None:
        with self.assertRaises(ValueError):
            parse_tags(" ,  , ")


if __name__ == "__main__":
    unittest.main()

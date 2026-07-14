from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from keys import canonical_key


class CanonicalKeyTests(unittest.TestCase):
    def test_collapses_two_spaces(self) -> None:
        self.assertEqual(canonical_key("  Alpha  Beta  "), "alpha-beta")

    def test_rejects_blank_label(self) -> None:
        with self.assertRaises(ValueError):
            canonical_key("   ")


if __name__ == "__main__":
    unittest.main()

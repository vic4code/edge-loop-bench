from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from service import display_name


class DisplayNameTests(unittest.TestCase):
    def test_formats_existing_name(self) -> None:
        self.assertEqual(display_name({7: "Ada"}, 7), "ADA")

    def test_returns_unknown_for_missing_id(self) -> None:
        self.assertEqual(display_name({7: "Ada"}, 9), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()

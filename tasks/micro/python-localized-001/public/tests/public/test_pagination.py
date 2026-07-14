from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from pagination import clamp_page


class ClampPageTests(unittest.TestCase):
    def test_page_above_range_clamps_to_last_page(self) -> None:
        self.assertEqual(clamp_page(99, 5), 5)

    def test_page_below_range_clamps_to_first_page(self) -> None:
        self.assertEqual(clamp_page(-3, 5), 1)


if __name__ == "__main__":
    unittest.main()

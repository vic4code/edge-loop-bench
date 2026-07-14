from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from pagination import clamp_page


class ClampPageHiddenTests(unittest.TestCase):
    def test_single_page_is_valid(self) -> None:
        self.assertEqual(clamp_page(1, 1), 1)

    def test_interior_page_is_unchanged(self) -> None:
        self.assertEqual(clamp_page(3, 5), 3)

    def test_non_positive_total_is_rejected(self) -> None:
        for total_pages in (0, -1):
            with self.subTest(total_pages=total_pages):
                with self.assertRaises(ValueError):
                    clamp_page(1, total_pages)


if __name__ == "__main__":
    unittest.main()

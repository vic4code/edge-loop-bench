from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from repository import lookup_name
from service import display_name


class DisplayNameHiddenTests(unittest.TestCase):
    def test_repository_contract_is_preserved(self) -> None:
        self.assertEqual(lookup_name({1: "Lin"}, 1), (True, "Lin"))
        self.assertEqual(lookup_name({}, 1), (False, None))

    def test_empty_stored_name_is_not_absence(self) -> None:
        self.assertEqual(display_name({1: ""}, 1), "")


if __name__ == "__main__":
    unittest.main()

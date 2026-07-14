from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from audit import total_units
from inventory import Inventory


class InventoryTests(unittest.TestCase):
    def test_reservation_updates_both_pools(self) -> None:
        inventory = Inventory(available=10)
        inventory.reserve(3)
        self.assertEqual((inventory.available, inventory.reserved), (7, 3))
        self.assertEqual(total_units(inventory), 10)


if __name__ == "__main__":
    unittest.main()

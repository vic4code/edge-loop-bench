from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path.cwd() / "src"))

from audit import total_units
from inventory import Inventory


class InventoryHiddenTests(unittest.TestCase):
    def test_multiple_reservations_conserve_total(self) -> None:
        inventory = Inventory(available=8, reserved=2)
        inventory.reserve(3)
        inventory.reserve(1)
        self.assertEqual((inventory.available, inventory.reserved), (4, 6))
        self.assertEqual(total_units(inventory), 10)

    def test_invalid_reservation_does_not_mutate(self) -> None:
        inventory = Inventory(available=2, reserved=1)
        with self.assertRaises(ValueError):
            inventory.reserve(3)
        self.assertEqual((inventory.available, inventory.reserved), (2, 1))


if __name__ == "__main__":
    unittest.main()

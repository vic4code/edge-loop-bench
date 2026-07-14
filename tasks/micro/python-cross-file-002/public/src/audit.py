"""Read-only inventory audit helpers."""

from inventory import Inventory


def total_units(inventory: Inventory) -> int:
    """Return all available and reserved units."""

    return inventory.available + inventory.reserved

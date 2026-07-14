"""Inventory state transitions."""

from dataclasses import dataclass


@dataclass
class Inventory:
    available: int
    reserved: int = 0

    def reserve(self, quantity: int) -> None:
        """Move *quantity* available units into the reserved pool."""

        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if quantity > self.available:
            raise ValueError("insufficient inventory")
        self.available -= quantity

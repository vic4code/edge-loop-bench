# Repair task: v03-cross-file-005

delivery_cost(weight, express) multiplies tariff.rate_per_kg by non-negative numeric weight and rounds to two decimals; invalid weight raises ValueError.

Fix `src/`; do not modify tests.

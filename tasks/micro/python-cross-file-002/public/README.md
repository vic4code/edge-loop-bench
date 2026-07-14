# Repair task: inventory reservation state

`Inventory.reserve(quantity)` moves units from `available` to `reserved`.
Both fields and the total reported by `audit.total_units` must remain
consistent. Invalid quantities must not mutate state.

Fix the implementation under `src/`. Do not modify tests.

Run `python3 -m unittest discover -s tests/public -v`.

# Repair task: pagination upper bound

`clamp_page(page, total_pages)` must return a valid one-based page number.

- values below page 1 clamp to 1;
- values above the last page clamp to `total_pages`;
- a non-positive `total_pages` value raises `ValueError`.

Fix the implementation under `src/`. Do not modify tests.

Run the public tests with:

```bash
python3 -m unittest discover -s tests/public -v
```

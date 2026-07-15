#!/usr/bin/env python3
"""Generate the frozen v0.4 official-loop qualification pilot."""

from __future__ import annotations

from generate_v03_tasks import CrossSpec, ROOT, S, _materialize, _source


SUITE = "official-loop-pilot"

SINGLES = (
    S(SUITE, "v04-localized-001", "localized",
      "page_count(total_items, page_size) returns ceiling division, returns zero for no items, and rejects negative totals or non-positive page sizes.",
      "page_count(total_items, page_size)",
      "if page_size <= 0:\n        raise ValueError('invalid size')\n    return total_items // page_size",
      "if page_size <= 0 or total_items < 0:\n        raise ValueError('invalid pagination')\n    return (total_items + page_size - 1) // page_size",
      ("self.assertEqual(page_count(11, 5), 3)",),
      ("self.assertEqual(page_count(0, 5), 0)", "with self.assertRaises(ValueError): page_count(-1, 5)", "with self.assertRaises(ValueError): page_count(1, 0)")),
    S(SUITE, "v04-localized-002", "localized",
      "normalize_key(text) trims, lowercases, joins every non-empty whitespace run with one underscore, and rejects blank input.",
      "normalize_key(text)",
      "value = text.strip().lower()\n    if not value:\n        raise ValueError('blank')\n    return value.replace('  ', '_').replace(' ', '_')",
      "if not isinstance(text, str):\n        raise ValueError('text required')\n    value = text.strip().lower()\n    if not value:\n        raise ValueError('blank')\n    return '_'.join(value.split())",
      ("self.assertEqual(normalize_key(' Alpha   Beta '), 'alpha_beta')",),
      ("self.assertEqual(normalize_key('A\\t B\\nC'), 'a_b_c')", "with self.assertRaises(ValueError): normalize_key('  ')", "with self.assertRaises(ValueError): normalize_key(None)")),
    S(SUITE, "v04-localized-003", "localized",
      "clamp_ratio(value) accepts finite numeric values excluding booleans and clamps them to the inclusive range 0.0..1.0.",
      "clamp_ratio(value)",
      "return max(0.0, min(float(value), 0.99))",
      "import math\n    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):\n        raise ValueError('finite number required')\n    return max(0.0, min(float(value), 1.0))",
      ("self.assertEqual(clamp_ratio(1), 1.0)",),
      ("self.assertEqual(clamp_ratio(-2), 0.0)", "with self.assertRaises(ValueError): clamp_ratio(True)", "with self.assertRaises(ValueError): clamp_ratio(float('inf'))")),
    S(SUITE, "v04-diagnosis-001", "diagnosis",
      "active_total(rows) sums finite numeric amount fields from active dict rows, ignoring malformed rows and excluding booleans.",
      "active_total(rows)",
      "return sum(row['amount'] for row in rows if row['active'])",
      "import math\n    total = 0\n    for row in rows:\n        if not isinstance(row, dict) or row.get('active') is not True:\n            continue\n        amount = row.get('amount')\n        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount):\n            total += amount\n    return total",
      ("self.assertEqual(active_total([None, {'active': True, 'amount': 4}, {'active': False, 'amount': 9}]), 4)",),
      ("self.assertEqual(active_total([{'active': True, 'amount': True}, {'active': True, 'amount': 2.5}, {}]), 2.5)",)),
    S(SUITE, "v04-diagnosis-002", "diagnosis",
      "first_valid_email(rows) returns the first trimmed lower-case non-empty email string from dict rows containing exactly one non-edge '@' and no whitespace, or None.",
      "first_valid_email(rows)",
      "for row in rows:\n        value = row['email'].strip().lower()\n        if value:\n            return value\n    return None",
      "for row in rows:\n        if not isinstance(row, dict) or not isinstance(row.get('email'), str):\n            continue\n        value = row['email'].strip().lower()\n        if value.count('@') != 1 or any(ch.isspace() for ch in value):\n            continue\n        local, domain = value.split('@')\n        if local and domain:\n            return value\n    return None",
      ("self.assertEqual(first_valid_email([None, {'email': 'bad value'}, {'email': ' User@Host.COM '}]), 'user@host.com')",),
      ("self.assertIsNone(first_valid_email([{}, {'email': '@host'}, {'email': 'a@@b'}]))",)),
    S(SUITE, "v04-adversarial-001", "adversarial",
      "safe_relative(parts) joins trimmed non-empty string segments with '/', rejecting absolute segments, '..' traversal, and input with no usable segment.",
      "safe_relative(parts)",
      "usable = [part.strip() for part in parts if part]\n    return '/'.join(usable)",
      "usable = []\n    for part in parts:\n        if not isinstance(part, str):\n            continue\n        value = part.strip()\n        if not value:\n            continue\n        if value == '..' or value.startswith(('/', '\\\\')):\n            raise ValueError('unsafe path')\n        usable.append(value)\n    if not usable:\n        raise ValueError('empty path')\n    return '/'.join(usable)",
      ("with self.assertRaises(ValueError): safe_relative(['api', '..', 'secret'])",),
      ("self.assertEqual(safe_relative([' api ', None, '', 'v1']), 'api/v1')", "with self.assertRaises(ValueError): safe_relative(['/root'])", "with self.assertRaises(ValueError): safe_relative([' '])"),
      source_type="verifier_adversarial"),
)

CROSSES = (
    CrossSpec(SUITE, "v04-cross-file-001", "generated_mutation",
      "final_price(subtotal, tier) applies policy.discount_rate as a fraction of subtotal, rounds to two decimals, and rejects negative subtotals.",
      {"policy.py": "def discount_rate(tier):\n    return {'standard': 0.0, 'member': 0.1}.get(tier, 0.0)\n",
       "service.py": "from policy import discount_rate\n\ndef final_price(subtotal, tier):\n    return round(subtotal - discount_rate(tier), 2)\n"},
      {"service.py": "from policy import discount_rate\n\ndef final_price(subtotal, tier):\n    if subtotal < 0:\n        raise ValueError('negative subtotal')\n    return round(subtotal * (1 - discount_rate(tier)), 2)\n"},
      ("self.assertEqual(final_price(100, 'member'), 90.0)",),
      ("self.assertEqual(final_price(12.34, 'standard'), 12.34)", "with self.assertRaises(ValueError): final_price(-1, 'member')")),
    CrossSpec(SUITE, "v04-cross-file-002", "generated_mutation",
      "record_lookup(records, key) returns (True, value) for every present key including None, False, or zero, otherwise (False, None), matching repository.has_key.",
      {"repository.py": "def has_key(records, key):\n    return key in records\n",
       "service.py": "from repository import has_key\n\ndef record_lookup(records, key):\n    value = records.get(key)\n    return (bool(value), value)\n"},
      {"service.py": "from repository import has_key\n\ndef record_lookup(records, key):\n    if not has_key(records, key):\n        return (False, None)\n    return (True, records[key])\n"},
      ("self.assertEqual(record_lookup({'x': 0}, 'x'), (True, 0))",),
      ("self.assertEqual(record_lookup({'x': None}, 'x'), (True, None))", "self.assertEqual(record_lookup({}, 'x'), (False, None))")),
)


def main() -> None:
    task_suite = ROOT / "tasks" / SUITE
    evaluator_suite = ROOT / "evaluators" / SUITE
    if task_suite.exists() or evaluator_suite.exists():
        raise RuntimeError(f"refusing to overwrite suite {SUITE}")
    for spec in SINGLES:
        _materialize(
            spec.suite, spec.task_id, spec.category, spec.source_type,
            spec.instruction,
            {"solution.py": _source(spec.signature, spec.buggy_body)},
            {"solution.py": _source(spec.signature, spec.fixed_body)},
            spec.public_checks, spec.hidden_checks, "from solution import *",
        )
    for spec in CROSSES:
        _materialize(
            spec.suite, spec.task_id, "cross-file", spec.source_type,
            spec.instruction, spec.initial_files, spec.fixed_files,
            spec.public_checks, spec.hidden_checks, "from service import *",
        )
    for provenance in task_suite.glob("*/provenance.md"):
        category = provenance.parent.name.split("-")[1]
        provenance.write_text(
            f"Original offline {category} mutation generated for EdgeLoopBench v0.4.\n"
        )
    count = len(list(task_suite.glob("*/task.toml")))
    if count != 8:
        raise RuntimeError(f"unexpected suite size: {count}")
    print(f"generated {count} v0.4 pilot tasks")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate the preregistered v0.3 offline repair suites deterministically."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LICENSE = (ROOT / "tasks/confirmatory/confirm-localized-001/public/LICENSE").read_text()
@dataclass(frozen=True)
class SingleSpec:
    suite: str
    task_id: str
    category: str
    source_type: str
    instruction: str
    signature: str
    buggy_body: str
    fixed_body: str
    public_checks: tuple[str, ...]
    hidden_checks: tuple[str, ...]


@dataclass(frozen=True)
class CrossSpec:
    suite: str
    task_id: str
    source_type: str
    instruction: str
    initial_files: dict[str, str]
    fixed_files: dict[str, str]
    public_checks: tuple[str, ...]
    hidden_checks: tuple[str, ...]


def S(
    suite: str, task_id: str, category: str, instruction: str,
    signature: str, buggy: str, fixed: str,
    public: tuple[str, ...], hidden: tuple[str, ...],
    source_type: str = "generated_mutation",
) -> SingleSpec:
    return SingleSpec(
        suite, task_id, category, source_type, instruction, signature,
        buggy, fixed, public, hidden,
    )


SINGLES = (
    # Disjoint calibration tasks. These may shape prompts and budgets, never endpoints.
    S("topology-calibration", "cal3-localized-001", "localized",
      "bounded_score(value, maximum) clamps to the inclusive range 0..maximum and rejects a negative maximum.",
      "bounded_score(value, maximum)",
      "if maximum < 0:\n        raise ValueError('negative maximum')\n    return max(0, min(value, maximum - 1))",
      "if maximum < 0:\n        raise ValueError('negative maximum')\n    return max(0, min(value, maximum))",
      ("self.assertEqual(bounded_score(8, 8), 8)",),
      ("self.assertEqual(bounded_score(-2, 8), 0)", "with self.assertRaises(ValueError): bounded_score(1, -1)")),
    S("topology-calibration", "cal3-localized-002", "localized",
      "compact_code(text) trims, lowercases, and replaces each non-empty whitespace run with one underscore; blank input is invalid.",
      "compact_code(text)",
      "value = text.strip().lower()\n    if not value:\n        raise ValueError('blank code')\n    return value.replace(' ', '_')",
      "value = text.strip().lower()\n    if not value:\n        raise ValueError('blank code')\n    return '_'.join(value.split())",
      ("self.assertEqual(compact_code('A   B'), 'a_b')",),
      ("self.assertEqual(compact_code(' A\\tB\\nC '), 'a_b_c')", "with self.assertRaises(ValueError): compact_code('  ')") ,
      source_type="verifier_adversarial"),
    S("topology-calibration", "cal3-diagnosis-001", "diagnosis",
      "accepted_total(rows) sums numeric amount fields only for rows whose status is 'accepted'; malformed rows are ignored.",
      "accepted_total(rows)",
      "total = 0\n    for row in rows:\n        if row.get('status') == 'accepted':\n            total += row.get('amount', 0)\n    return total",
      "total = 0\n    for row in rows:\n        if not isinstance(row, dict) or row.get('status') != 'accepted':\n            continue\n        amount = row.get('amount')\n        if isinstance(amount, (int, float)) and not isinstance(amount, bool):\n            total += amount\n    return total",
      ("self.assertEqual(accepted_total([None, {'status': 'accepted', 'amount': 4}, {'status': 'rejected', 'amount': 9}]), 4)",),
      ("self.assertEqual(accepted_total([None, {'status': 'accepted', 'amount': 'x'}, {'status': 'accepted', 'amount': 2.5}]), 2.5)",)),
    S("topology-calibration", "cal3-adversarial-001", "adversarial",
      "canonical_path(parts) joins non-blank trimmed path segments with '/', rejecting input with no usable segment.",
      "canonical_path(parts)",
      "usable = [part.strip() for part in parts if part]\n    if not usable:\n        raise ValueError('empty path')\n    return '/'.join(usable)",
      "usable = [part.strip() for part in parts if isinstance(part, str) and part.strip()]\n    if not usable:\n        raise ValueError('empty path')\n    return '/'.join(usable)",
      ("self.assertEqual(canonical_path([' api ', ' ', 'v1']), 'api/v1')",),
      ("self.assertEqual(canonical_path(['a', None, ' b ']), 'a/b')", "with self.assertRaises(ValueError): canonical_path([' ', ''])"),
      source_type="verifier_adversarial"),

    # ConfirmatoryRepair-B-30: twelve localized tasks.
    S("confirmatory-b", "v03-localized-001", "localized",
      "cap_inclusive(value, low, high) clamps to inclusive bounds and rejects low > high.",
      "cap_inclusive(value, low, high)",
      "if low > high:\n        raise ValueError('invalid bounds')\n    return max(low, min(value, high - 1))",
      "if low > high:\n        raise ValueError('invalid bounds')\n    return max(low, min(value, high))",
      ("self.assertEqual(cap_inclusive(12, 1, 12), 12)",),
      ("self.assertEqual(cap_inclusive(-3, -1, 8), -1)", "with self.assertRaises(ValueError): cap_inclusive(0, 2, 1)")),
    S("confirmatory-b", "v03-localized-002", "localized",
      "batch_count(item_count, batch_size) returns ceiling division, returns 0 for no items, and rejects non-positive batch sizes.",
      "batch_count(item_count, batch_size)",
      "if batch_size <= 0:\n        raise ValueError('invalid size')\n    return item_count // batch_size",
      "if batch_size <= 0:\n        raise ValueError('invalid size')\n    if item_count < 0:\n        raise ValueError('negative count')\n    return (item_count + batch_size - 1) // batch_size",
      ("self.assertEqual(batch_count(11, 5), 3)",),
      ("self.assertEqual(batch_count(0, 5), 0)", "with self.assertRaises(ValueError): batch_count(-1, 5)", "with self.assertRaises(ValueError): batch_count(2, 0)")),
    S("confirmatory-b", "v03-localized-003", "localized",
      "parse_switch(value) accepts booleans or trimmed case-insensitive yes/no, true/false, 1/0 strings and rejects everything else.",
      "parse_switch(value)",
      "if isinstance(value, bool):\n        return value\n    return value in {'yes', 'true', '1'}",
      "if isinstance(value, bool):\n        return value\n    if not isinstance(value, str):\n        raise ValueError('invalid switch')\n    normalized = value.strip().lower()\n    if normalized in {'yes', 'true', '1'}:\n        return True\n    if normalized in {'no', 'false', '0'}:\n        return False\n    raise ValueError('invalid switch')",
      ("self.assertTrue(parse_switch(' YES '))", "self.assertFalse(parse_switch('False'))"),
      ("self.assertTrue(parse_switch(True))", "with self.assertRaises(ValueError): parse_switch('maybe')", "with self.assertRaises(ValueError): parse_switch(1)")),
    S("confirmatory-b", "v03-localized-004", "localized",
      "stable_unique(values) removes duplicates while preserving first-seen order and supports unhashable values.",
      "stable_unique(values)",
      "return list(set(values))",
      "result = []\n    for value in values:\n        if value not in result:\n            result.append(value)\n    return result",
      ("self.assertEqual(stable_unique([3, 1, 3, 2]), [3, 1, 2])",),
      ("self.assertEqual(stable_unique([[1], [1], [2]]), [[1], [2]])", "self.assertEqual(stable_unique([]), [])")),
    S("confirmatory-b", "v03-localized-005", "localized",
      "numeric_median(values) returns the numeric median as a float and rejects empty or non-numeric input, including booleans.",
      "numeric_median(values)",
      "ordered = sorted(values)\n    if not ordered:\n        raise ValueError('empty')\n    return float(ordered[len(ordered) // 2])",
      "if not values or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):\n        raise ValueError('numeric values required')\n    ordered = sorted(values)\n    middle = len(ordered) // 2\n    if len(ordered) % 2:\n        return float(ordered[middle])\n    return (ordered[middle - 1] + ordered[middle]) / 2.0",
      ("self.assertEqual(numeric_median([4, 1, 2, 9]), 3.0)",),
      ("self.assertEqual(numeric_median([3, 1, 2]), 2.0)", "with self.assertRaises(ValueError): numeric_median([])", "with self.assertRaises(ValueError): numeric_median([True, 2])")),
    S("confirmatory-b", "v03-localized-006", "localized",
      "inclusive_window(values, start, end) returns the inclusive slice and rejects negative, reversed, or out-of-range bounds.",
      "inclusive_window(values, start, end)",
      "if start < 0 or end < start:\n        raise ValueError('invalid window')\n    return values[start:end]",
      "if start < 0 or end < start or end >= len(values):\n        raise ValueError('invalid window')\n    return values[start:end + 1]",
      ("self.assertEqual(inclusive_window(['a', 'b', 'c'], 1, 2), ['b', 'c'])",),
      ("self.assertEqual(inclusive_window([1], 0, 0), [1])", "with self.assertRaises(ValueError): inclusive_window([1], 0, 1)", "with self.assertRaises(ValueError): inclusive_window([1], 1, 0)")),
    S("confirmatory-b", "v03-localized-007", "localized",
      "normalize_percent(value) converts a numeric value to float within inclusive 0..100 and rejects booleans and values outside the range.",
      "normalize_percent(value)",
      "number = float(value)\n    return max(0.0, min(number, 99.0))",
      "if not isinstance(value, (int, float)) or isinstance(value, bool):\n        raise ValueError('numeric percent required')\n    number = float(value)\n    if not 0.0 <= number <= 100.0:\n        raise ValueError('percent out of range')\n    return number",
      ("self.assertEqual(normalize_percent(100), 100.0)",),
      ("self.assertEqual(normalize_percent(0), 0.0)", "with self.assertRaises(ValueError): normalize_percent(-1)", "with self.assertRaises(ValueError): normalize_percent(True)")),
    S("confirmatory-b", "v03-localized-008", "localized",
      "retry_delay(base, attempt, maximum) returns min(maximum, base * 2**attempt) for non-negative attempt and positive base/maximum.",
      "retry_delay(base, attempt, maximum)",
      "if base <= 0 or maximum <= 0 or attempt < 0:\n        raise ValueError('invalid retry settings')\n    return min(maximum, base * 2 ** (attempt - 1))",
      "if base <= 0 or maximum <= 0 or attempt < 0:\n        raise ValueError('invalid retry settings')\n    return min(maximum, base * 2 ** attempt)",
      ("self.assertEqual(retry_delay(2, 0, 30), 2)", "self.assertEqual(retry_delay(2, 3, 10), 10)"),
      ("self.assertEqual(retry_delay(1.5, 2, 20), 6.0)", "with self.assertRaises(ValueError): retry_delay(1, -1, 3)")),
    S("confirmatory-b", "v03-localized-009", "localized",
      "chunk_at(values, index, size) returns exactly one size-wide chunk and rejects invalid size or an index outside the available chunks.",
      "chunk_at(values, index, size)",
      "if size <= 0 or index < 0:\n        raise ValueError('invalid chunk')\n    start = index * size\n    return values[start:start + size - 1]",
      "if size <= 0 or index < 0:\n        raise ValueError('invalid chunk')\n    start = index * size\n    if start >= len(values):\n        raise ValueError('chunk out of range')\n    return values[start:start + size]",
      ("self.assertEqual(chunk_at([1, 2, 3, 4], 0, 2), [1, 2])",),
      ("self.assertEqual(chunk_at([1, 2, 3], 1, 2), [3])", "with self.assertRaises(ValueError): chunk_at([1], 1, 1)")),
    S("confirmatory-b", "v03-localized-010", "localized",
      "file_extension(name) returns the lower-case suffix after the final dot, or an empty string for dotfiles, trailing dots, and names without extensions.",
      "file_extension(name)",
      "return name.split('.', 1)[-1].lower() if '.' in name else ''",
      "if not isinstance(name, str):\n        raise ValueError('name must be text')\n    base = name.rsplit('/', 1)[-1]\n    if base.startswith('.') and base.count('.') == 1:\n        return ''\n    if '.' not in base or base.endswith('.'):\n        return ''\n    return base.rsplit('.', 1)[1].lower()",
      ("self.assertEqual(file_extension('archive.tar.GZ'), 'gz')",),
      ("self.assertEqual(file_extension('.env'), '')", "self.assertEqual(file_extension('name.'), '')", "self.assertEqual(file_extension('dir/a.TXT'), 'txt')"),
      source_type="verifier_adversarial"),
    S("confirmatory-b", "v03-localized-011", "localized",
      "safe_average(values) averages finite numeric values, ignores None, and rejects empty effective input, booleans, and non-numeric values.",
      "safe_average(values)",
      "usable = [value for value in values if value is not None]\n    return sum(usable) / len(usable)",
      "usable = []\n    for value in values:\n        if value is None:\n            continue\n        if not isinstance(value, (int, float)) or isinstance(value, bool):\n            raise ValueError('numeric values required')\n        usable.append(value)\n    if not usable:\n        raise ValueError('no values')\n    return sum(usable) / len(usable)",
      ("with self.assertRaises(ValueError): safe_average([None])",),
      ("self.assertEqual(safe_average([2, None, 4]), 3.0)", "with self.assertRaises(ValueError): safe_average([True, 2])")),
    S("confirmatory-b", "v03-localized-012", "localized",
      "normalize_address(value) trims and lowercases the local/domain parts, requires exactly one non-edge '@', and rejects surrounding internal whitespace.",
      "normalize_address(value)",
      "return value.strip().lower()",
      "if not isinstance(value, str):\n        raise ValueError('address must be text')\n    normalized = value.strip().lower()\n    if normalized.count('@') != 1:\n        raise ValueError('invalid address')\n    local, domain = normalized.split('@')\n    if not local or not domain or any(ch.isspace() for ch in normalized):\n        raise ValueError('invalid address')\n    return normalized",
      ("with self.assertRaises(ValueError): normalize_address('a @host')",),
      ("self.assertEqual(normalize_address(' User@Example.COM '), 'user@example.com')", "with self.assertRaises(ValueError): normalize_address('missing')", "with self.assertRaises(ValueError): normalize_address('@host')"),
      source_type="verifier_adversarial"),

    # Six diagnosis tasks.
    S("confirmatory-b", "v03-diagnosis-001", "diagnosis",
      "sum_kind(rows, kind) sums numeric amount fields for matching dict rows and ignores malformed rows without printing.",
      "sum_kind(rows, kind)",
      "total = 0\n    for row in rows:\n        print('processing', row)\n        if row['kind'] == kind:\n            total += row['amount']\n    return total",
      "total = 0\n    for row in rows:\n        if not isinstance(row, dict) or row.get('kind') != kind:\n            continue\n        amount = row.get('amount')\n        if isinstance(amount, (int, float)) and not isinstance(amount, bool):\n            total += amount\n    return total",
      ("self.assertEqual(sum_kind([None, {'kind': 'a', 'amount': 2}, {'kind': 'b', 'amount': 9}], 'a'), 2)",),
      ("self.assertEqual(sum_kind([None, {'kind': 'a'}, {'kind': 'a', 'amount': 1.5}], 'a'), 1.5)",)),
    S("confirmatory-b", "v03-diagnosis-002", "diagnosis",
      "valid_ids(rows) returns integer ids from active dict rows in input order, skipping malformed, boolean, duplicate, and inactive ids.",
      "valid_ids(rows)",
      "return [row['id'] for row in rows if row.get('active')]",
      "result = []\n    for row in rows:\n        if not isinstance(row, dict) or row.get('active') is not True:\n            continue\n        value = row.get('id')\n        if isinstance(value, int) and not isinstance(value, bool) and value not in result:\n            result.append(value)\n    return result",
      ("self.assertEqual(valid_ids([None, {'id': 2, 'active': True}, {'id': 1, 'active': False}]), [2])",),
      ("self.assertEqual(valid_ids([None, {'id': True, 'active': True}, {'id': 3, 'active': True}, {'id': 3, 'active': True}]), [3])",)),
    S("confirmatory-b", "v03-diagnosis-003", "diagnosis",
      "latest_by_key(rows, key) returns the row with the greatest numeric 'version' matching key, or None when none is valid.",
      "latest_by_key(rows, key)",
      "matches = [row for row in rows if row['key'] == key]\n    return matches[-1] if matches else None",
      "best = None\n    for row in rows:\n        if not isinstance(row, dict) or row.get('key') != key:\n            continue\n        version = row.get('version')\n        if not isinstance(version, (int, float)) or isinstance(version, bool):\n            continue\n        if best is None or version > best['version']:\n            best = row\n    return best",
      ("self.assertEqual(latest_by_key([{'key': 'x', 'version': 3}, {'key': 'x', 'version': 1}], 'x')['version'], 3)",),
      ("self.assertIsNone(latest_by_key([None, {'key': 'x', 'version': 'new'}], 'x'))",)),
    S("confirmatory-b", "v03-diagnosis-004", "diagnosis",
      "status_counts(rows) counts trimmed lower-case non-empty string statuses from dict rows and ignores malformed entries.",
      "status_counts(rows)",
      "counts = {}\n    for row in rows:\n        status = row['status'].lower()\n        counts[status] = counts.get(status, 0) + 1\n    return counts",
      "counts = {}\n    for row in rows:\n        if not isinstance(row, dict) or not isinstance(row.get('status'), str):\n            continue\n        status = row['status'].strip().lower()\n        if not status:\n            continue\n        counts[status] = counts.get(status, 0) + 1\n    return counts",
      ("self.assertEqual(status_counts([{'status': ' OK '}, {'status': 'ok'}]), {'ok': 2})",),
      ("self.assertEqual(status_counts([None, {}, {'status': ' '}]), {})",)),
    S("confirmatory-b", "v03-diagnosis-005", "diagnosis",
      "reconcile(available, reserved) returns their total when both are non-negative integers excluding booleans; otherwise it raises ValueError.",
      "reconcile(available, reserved)",
      "if available < 0:\n        raise ValueError('negative')\n    return available + reserved",
      "if (not isinstance(available, int) or isinstance(available, bool) or\n            not isinstance(reserved, int) or isinstance(reserved, bool) or\n            available < 0 or reserved < 0):\n        raise ValueError('non-negative integers required')\n    return available + reserved",
      ("with self.assertRaises(ValueError): reconcile(2, -1)",),
      ("self.assertEqual(reconcile(2, 3), 5)", "with self.assertRaises(ValueError): reconcile(True, 2)")),
    S("confirmatory-b", "v03-diagnosis-006", "diagnosis",
      "group_values(rows) maps each non-empty string group to a list of present values in input order, ignoring malformed rows.",
      "group_values(rows)",
      "result = {}\n    for row in rows:\n        result.setdefault(row['group'], []).append(row['value'])\n    return result",
      "result = {}\n    for row in rows:\n        if not isinstance(row, dict) or not isinstance(row.get('group'), str):\n            continue\n        group = row['group'].strip()\n        if not group or 'value' not in row:\n            continue\n        result.setdefault(group, []).append(row['value'])\n    return result",
      ("self.assertEqual(group_values([None, {'group': ' a ', 'value': 1}, {'group': 'a', 'value': 2}]), {'a': [1, 2]})",),
      ("self.assertEqual(group_values([None, {'group': ' ', 'value': 1}, {'group': ' b ', 'value': 3}, {'group': 'b'}]), {'b': [3]})",)),

    # Four verifier-adversarial tasks.
    S("confirmatory-b", "v03-adversarial-001", "adversarial",
      "label_key(text) trims, lowercases, collapses every non-empty whitespace run to one dash, and rejects blank labels.",
      "label_key(text)",
      "value = text.strip().lower()\n    if not value:\n        raise ValueError('blank label')\n    return value.replace('  ', '-').replace(' ', '-')",
      "value = text.strip().lower()\n    if not value:\n        raise ValueError('blank label')\n    return '-'.join(value.split())",
      ("self.assertEqual(label_key('A   B'), 'a-b')",),
      ("self.assertEqual(label_key(' A\\t  B\\nC '), 'a-b-c')", "with self.assertRaises(ValueError): label_key('\\t ')") ,
      source_type="verifier_adversarial"),
    S("confirmatory-b", "v03-adversarial-002", "adversarial",
      "redact_secret(text, secret) replaces exact non-empty secret occurrences with '[REDACTED]' and rejects a blank secret without altering partial matches.",
      "redact_secret(text, secret)",
      "return text.replace(secret.strip(), '[REDACTED]')",
      "if not isinstance(secret, str) or not secret:\n        raise ValueError('secret required')\n    return text.replace(secret, '[REDACTED]')",
      ("with self.assertRaises(ValueError): redact_secret('abc', '')",),
      ("self.assertEqual(redact_secret('key=abc', 'abc'), 'key=[REDACTED]')", "self.assertEqual(redact_secret('xabcx', ' abc '), 'xabcx')"),
      source_type="verifier_adversarial"),
    S("confirmatory-b", "v03-adversarial-003", "adversarial",
      "merge_ranges(ranges) sorts and merges overlapping or touching inclusive integer ranges, rejecting reversed or non-integer endpoints.",
      "merge_ranges(ranges)",
      "ordered = sorted(ranges)\n    merged = []\n    for start, end in ordered:\n        if merged and start <= merged[-1][1]:\n            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))\n        else:\n            merged.append((start, end))\n    return merged",
      "ordered = []\n    for item in ranges:\n        if (not isinstance(item, (tuple, list)) or len(item) != 2 or\n                any(not isinstance(v, int) or isinstance(v, bool) for v in item)):\n            raise ValueError('integer ranges required')\n        start, end = item\n        if start > end:\n            raise ValueError('reversed range')\n        ordered.append((start, end))\n    ordered.sort()\n    merged = []\n    for start, end in ordered:\n        if merged and start <= merged[-1][1] + 1:\n            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))\n        else:\n            merged.append((start, end))\n    return merged",
      ("self.assertEqual(merge_ranges([(1, 2), (3, 5)]), [(1, 5)])",),
      ("self.assertEqual(merge_ranges([(5, 7), (1, 2)]), [(1, 2), (5, 7)])", "with self.assertRaises(ValueError): merge_ranges([(3, 2)])"),
      source_type="verifier_adversarial"),
    S("confirmatory-b", "v03-adversarial-004", "adversarial",
      "relative_key(path) normalizes backslashes to '/', removes '.' segments, rejects '..' traversal and absolute paths, and returns a non-empty relative key.",
      "relative_key(path)",
      "parts = path.replace('\\\\', '/').split('/')\n    return '/'.join(part for part in parts if part not in {'', '.'})",
      "if not isinstance(path, str) or not path or path.startswith(('/', '\\\\')):\n        raise ValueError('relative path required')\n    parts = path.replace('\\\\', '/').split('/')\n    if any(part == '..' for part in parts):\n        raise ValueError('traversal is forbidden')\n    usable = [part for part in parts if part not in {'', '.'}]\n    if not usable:\n        raise ValueError('empty key')\n    return '/'.join(usable)",
      ("with self.assertRaises(ValueError): relative_key('../a')",),
      ("self.assertEqual(relative_key('a/./b'), 'a/b')", "with self.assertRaises(ValueError): relative_key('/a')", "self.assertEqual(relative_key('a\\\\b'), 'a/b')"),
      source_type="verifier_adversarial"),
)


CROSSES = (
    CrossSpec("topology-calibration", "cal3-cross-file-001", "generated_mutation",
      "shipping_quote(subtotal, priority) applies policy.shipping_rate and rounds the final quote to two decimals.",
      {"policy.py": "def shipping_rate(subtotal, priority):\n    return 0.05 if priority else 0.02\n",
       "service.py": "from policy import shipping_rate\n\ndef shipping_quote(subtotal, priority):\n    return subtotal + shipping_rate(subtotal, priority)\n"},
      {"service.py": "from policy import shipping_rate\n\ndef shipping_quote(subtotal, priority):\n    return round(subtotal * (1 + shipping_rate(subtotal, priority)), 2)\n"},
      ("self.assertEqual(shipping_quote(100, True), 105.0)",),
      ("self.assertEqual(shipping_quote(12.34, False), 12.59)",)),
    CrossSpec("topology-calibration", "cal3-cross-file-002", "generated_mutation",
      "consume_quota(store, name, units) decrements available quota only for a positive integer request that fits; invalid requests return False without mutation.",
      {"repository.py": "def available(store, name):\n    return store.get(name, 0)\n",
       "service.py": "from repository import available\n\ndef consume_quota(store, name, units):\n    if units <= available(store, name):\n        store[name] = available(store, name) - units\n        return True\n    return False\n"},
      {"service.py": "from repository import available\n\ndef consume_quota(store, name, units):\n    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:\n        return False\n    current = available(store, name)\n    if units > current:\n        return False\n    store[name] = current - units\n    return True\n"},
      ("store = {'api': 2}; self.assertFalse(consume_quota(store, 'api', -1)); self.assertEqual(store, {'api': 2})",),
      ("store = {'api': 2}; self.assertFalse(consume_quota(store, 'api', 0)); self.assertEqual(store, {'api': 2})", "store = {'api': 2}; self.assertTrue(consume_quota(store, 'api', 2)); self.assertEqual(store['api'], 0)")),

    # Eight confirmatory cross-file tasks.
    CrossSpec("confirmatory-b", "v03-cross-file-001", "generated_mutation",
      "invoice_total(subtotal, member) applies policy.rebate_rate to subtotal and rounds the final amount to two decimals.",
      {"policy.py": "def rebate_rate(subtotal, member):\n    return 0.08 if member and subtotal >= 50 else 0.0\n",
       "service.py": "from policy import rebate_rate\n\ndef invoice_total(subtotal, member):\n    return subtotal - rebate_rate(subtotal, member)\n"},
      {"service.py": "from policy import rebate_rate\n\ndef invoice_total(subtotal, member):\n    return round(subtotal * (1 - rebate_rate(subtotal, member)), 2)\n"},
      ("self.assertEqual(invoice_total(100, True), 92.0)",),
      ("self.assertEqual(invoice_total(49.99, True), 49.99)", "self.assertEqual(invoice_total(55.55, True), 51.11)")),
    CrossSpec("confirmatory-b", "v03-cross-file-002", "generated_mutation",
      "reserve_units(state, sku, units) moves a positive integer quantity from available to reserved only when it fits; failure returns False without mutation.",
      {"repository.py": "def counts(state, sku):\n    return state.setdefault(sku, {'available': 0, 'reserved': 0})\n",
       "service.py": "from repository import counts\n\ndef reserve_units(state, sku, units):\n    record = counts(state, sku)\n    record['available'] -= units\n    record['reserved'] += units\n    return record['available'] >= 0\n"},
      {"service.py": "from repository import counts\n\ndef reserve_units(state, sku, units):\n    if not isinstance(units, int) or isinstance(units, bool) or units <= 0:\n        return False\n    record = counts(state, sku)\n    if units > record['available']:\n        return False\n    record['available'] -= units\n    record['reserved'] += units\n    return True\n"},
      ("state = {'x': {'available': 2, 'reserved': 0}}; self.assertFalse(reserve_units(state, 'x', 3)); self.assertEqual(state['x']['available'], 2)",),
      ("state = {'x': {'available': 2, 'reserved': 1}}; self.assertTrue(reserve_units(state, 'x', 2)); self.assertEqual(state['x'], {'available': 0, 'reserved': 3})", "state = {'x': {'available': 2, 'reserved': 0}}; self.assertFalse(reserve_units(state, 'x', 0)); self.assertEqual(state['x']['available'], 2)")),
    CrossSpec("confirmatory-b", "v03-cross-file-003", "reconstructed_bug",
      "display_name(store, user_id) uses repository.lookup's (found, value) contract and returns a trimmed upper-case name or 'UNKNOWN'.",
      {"repository.py": "def lookup(store, user_id):\n    return (user_id in store, store.get(user_id))\n",
       "service.py": "from repository import lookup\n\ndef display_name(store, user_id):\n    value, found = lookup(store, user_id)\n    return value.strip().upper() if found else 'UNKNOWN'\n"},
      {"service.py": "from repository import lookup\n\ndef display_name(store, user_id):\n    found, value = lookup(store, user_id)\n    if not found or not isinstance(value, str) or not value.strip():\n        return 'UNKNOWN'\n    return value.strip().upper()\n"},
      ("self.assertEqual(display_name({1: ' Ada '}, 1), 'ADA')",),
      ("self.assertEqual(display_name({}, 1), 'UNKNOWN')", "self.assertEqual(display_name({1: None}, 1), 'UNKNOWN')")),
    CrossSpec("confirmatory-b", "v03-cross-file-004", "generated_mutation",
      "can_publish(user, document) requires policy.has_role(user, 'editor') and document status 'draft'; missing fields return False.",
      {"policy.py": "def has_role(user, role):\n    return role in user.get('roles', [])\n",
       "service.py": "from policy import has_role\n\ndef can_publish(user, document):\n    return has_role(user, 'editor') or document['status'] == 'draft'\n"},
      {"service.py": "from policy import has_role\n\ndef can_publish(user, document):\n    if not isinstance(user, dict) or not isinstance(document, dict):\n        return False\n    return has_role(user, 'editor') and document.get('status') == 'draft'\n"},
      ("self.assertFalse(can_publish({'roles': []}, {'status': 'draft'}))", "self.assertFalse(can_publish({'roles': ['editor']}, {'status': 'published'}))"),
      ("self.assertTrue(can_publish({'roles': ['editor']}, {'status': 'draft'}))", "self.assertFalse(can_publish({}, {}))")),
    CrossSpec("confirmatory-b", "v03-cross-file-005", "generated_mutation",
      "delivery_cost(weight, express) multiplies tariff.rate_per_kg by non-negative numeric weight and rounds to two decimals; invalid weight raises ValueError.",
      {"tariff.py": "def rate_per_kg(express):\n    return 3.5 if express else 2.0\n",
       "service.py": "from tariff import rate_per_kg\n\ndef delivery_cost(weight, express):\n    return round(weight + rate_per_kg(express), 2)\n"},
      {"service.py": "from tariff import rate_per_kg\n\ndef delivery_cost(weight, express):\n    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight < 0:\n        raise ValueError('invalid weight')\n    return round(weight * rate_per_kg(express), 2)\n"},
      ("self.assertEqual(delivery_cost(4, False), 8.0)",),
      ("self.assertEqual(delivery_cost(1.25, True), 4.38)", "with self.assertRaises(ValueError): delivery_cost(-1, False)")),
    CrossSpec("confirmatory-b", "v03-cross-file-006", "reconstructed_bug",
      "read_setting(store, key, default) respects repository.fetch's found/value contract, returning default only when absent or value is None.",
      {"repository.py": "def fetch(store, key):\n    return {'found': key in store, 'value': store.get(key)}\n",
       "service.py": "from repository import fetch\n\ndef read_setting(store, key, default):\n    result = fetch(store, key)\n    return result['value'] or default\n"},
      {"service.py": "from repository import fetch\n\ndef read_setting(store, key, default):\n    result = fetch(store, key)\n    if not result['found'] or result['value'] is None:\n        return default\n    return result['value']\n"},
      ("self.assertEqual(read_setting({'limit': 0}, 'limit', 10), 0)",),
      ("self.assertEqual(read_setting({'enabled': False}, 'enabled', True), False)", "self.assertEqual(read_setting({}, 'x', 'd'), 'd')")),
    CrossSpec("confirmatory-b", "v03-cross-file-007", "generated_mutation",
      "taxed_total(subtotal, region) applies tax.rate_for(region) to a non-negative numeric subtotal and rounds to two decimals.",
      {"tax.py": "def rate_for(region):\n    return {'north': 0.05, 'south': 0.08}.get(region, 0.0)\n",
       "service.py": "from tax import rate_for\n\ndef taxed_total(subtotal, region):\n    return subtotal + rate_for(region)\n"},
      {"service.py": "from tax import rate_for\n\ndef taxed_total(subtotal, region):\n    if not isinstance(subtotal, (int, float)) or isinstance(subtotal, bool) or subtotal < 0:\n        raise ValueError('invalid subtotal')\n    return round(subtotal * (1 + rate_for(region)), 2)\n"},
      ("self.assertEqual(taxed_total(100, 'south'), 108.0)",),
      ("self.assertEqual(taxed_total(12.34, 'north'), 12.96)", "with self.assertRaises(ValueError): taxed_total(-1, 'north')")),
    CrossSpec("confirmatory-b", "v03-cross-file-008", "generated_mutation",
      "cache_read(cache, key) returns (True, value) for any stored value including None/False/0, otherwise (False, None), matching repository.contains.",
      {"repository.py": "def contains(cache, key):\n    return key in cache\n",
       "service.py": "from repository import contains\n\ndef cache_read(cache, key):\n    value = cache.get(key)\n    return (bool(value), value)\n"},
      {"service.py": "from repository import contains\n\ndef cache_read(cache, key):\n    if not contains(cache, key):\n        return (False, None)\n    return (True, cache[key])\n"},
      ("self.assertEqual(cache_read({'x': 0}, 'x'), (True, 0))",),
      ("self.assertEqual(cache_read({'x': None}, 'x'), (True, None))", "self.assertEqual(cache_read({}, 'x'), (False, None))")),
)


def _source(signature: str, body: str) -> str:
    return f"def {signature}:\n    {body}\n"


def _test_module(import_line: str, checks: tuple[str, ...]) -> str:
    methods = []
    for index, check in enumerate(checks, 1):
        body = "\n        ".join(part.strip() for part in check.split("; "))
        methods.append(f"    def test_case_{index}(self):\n        {body}\n")
    return (
        "import sys\nimport unittest\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
        f"{import_line}\n\nclass BehaviorTests(unittest.TestCase):\n"
        + "\n".join(methods)
        + "\nif __name__ == '__main__':\n    unittest.main()\n"
    )


def _run_git(worktree: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": __import__("os").environ["PATH"],
        "GIT_AUTHOR_NAME": "EdgeLoopBench",
        "GIT_AUTHOR_EMAIL": "bench@example.invalid",
        "GIT_AUTHOR_DATE": "2026-07-14T00:00:00Z",
        "GIT_COMMITTER_NAME": "EdgeLoopBench",
        "GIT_COMMITTER_EMAIL": "bench@example.invalid",
        "GIT_COMMITTER_DATE": "2026-07-14T00:00:00Z",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *arguments],
        cwd=worktree, check=True, capture_output=True, text=True,
        env=environment,
    )


def _materialize(
    suite: str,
    task_id: str,
    category: str,
    source_type: str,
    instruction: str,
    initial_files: dict[str, str],
    fixed_files: dict[str, str],
    public_checks: tuple[str, ...],
    hidden_checks: tuple[str, ...],
    import_line: str,
) -> None:
    task_root = ROOT / "tasks" / suite / task_id
    evaluator_root = ROOT / "evaluators" / suite / task_id
    if task_root.exists() or evaluator_root.exists():
        raise RuntimeError(f"refusing to overwrite generated task {task_id}")
    public_root = task_root / "public"
    (public_root / "src").mkdir(parents=True)
    (public_root / "tests/public").mkdir(parents=True)
    (evaluator_root / "tests").mkdir(parents=True)
    (public_root / "README.md").write_text(
        f"# Repair task: {task_id}\n\n{instruction}\n\nFix `src/`; do not modify tests.\n"
    )
    (public_root / "LICENSE").write_text(LICENSE)
    (public_root / "requirements.lock").write_text("# Python standard library only.\n")
    for relative, content in initial_files.items():
        path = public_root / "src" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (public_root / "tests/public/test_behavior.py").write_text(
        _test_module(import_line, public_checks)
    )
    (evaluator_root / "tests/test_hidden.py").write_text(
        _test_module(import_line, hidden_checks)
    )

    with tempfile.TemporaryDirectory() as directory:
        worktree = Path(directory) / "worktree"
        shutil.copytree(public_root, worktree)
        _run_git(worktree, ["init", "-q", "--initial-branch=main"])
        _run_git(worktree, ["add", "--all"])
        _run_git(worktree, ["commit", "-q", "-m", f"Initialize {task_id}"])
        commit = _run_git(worktree, ["rev-parse", "HEAD"]).stdout.strip()
        for relative, content in fixed_files.items():
            (worktree / "src" / relative).write_text(content)
        patch = _run_git(worktree, ["diff", "--binary", "--", "src"]).stdout
    gold_path = evaluator_root / "gold.patch"
    gold_path.write_text(patch)
    gold_digest = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    task_root.joinpath("task.toml").write_text(
        "\n".join((
            "schema_version = 1",
            f'id = "{task_id}"',
            'language = "python"',
            f'category = "{category}"',
            f'source_type = "{source_type}"',
            'license = "MIT"',
            f'initial_commit = "{commit}"',
            f'gold_patch_sha256 = "sha256:{gold_digest}"',
            "",
            'allowed_paths = ["src/**"]',
            'prohibited_paths = ["tests/**", ".edgeloop/**"]',
            "",
            "[public_test]",
            'command = ["python3", "-m", "unittest", "discover", "-s", "tests/public"]',
            "timeout_seconds = 30",
            "",
            "[hidden_evaluation]",
            f'evaluator_id = "external-{task_id}"',
            "timeout_seconds = 30",
            "",
        ))
    )
    task_root.joinpath("provenance.md").write_text(
        f"Original offline {category} mutation generated for EdgeLoopBench v0.3.\n"
    )


def main() -> None:
    for suite in ("topology-calibration", "confirmatory-b"):
        task_suite = ROOT / "tasks" / suite
        evaluator_suite = ROOT / "evaluators" / suite
        if task_suite.exists() or evaluator_suite.exists():
            raise RuntimeError(f"refusing to overwrite suite {suite}")
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
    calibration = len(list((ROOT / "tasks/topology-calibration").glob("*/task.toml")))
    confirmatory = len(list((ROOT / "tasks/confirmatory-b").glob("*/task.toml")))
    if (calibration, confirmatory) != (6, 30):
        raise RuntimeError(f"unexpected suite sizes: {calibration}, {confirmatory}")
    print(f"generated {calibration} calibration and {confirmatory} confirmatory tasks")


if __name__ == "__main__":
    main()

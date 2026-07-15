#!/usr/bin/env python3
"""Deterministically build the frozen 30-task seeded-mutation suite."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from edgeloopbench.tasks import _run_git


ROOT = Path(__file__).parents[1]
TASK_ROOT = ROOT / "tasks" / "confirmatory"
EVALUATOR_ROOT = ROOT / "evaluators" / "confirmatory"
LICENSE = """MIT License

Copyright (c) 2026 EdgeLoopBench contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    requirement: str
    bug: str
    gold: str
    public: tuple[str, ...]
    hidden: tuple[str, ...]
    extra_bug: str | None = None
    extra_gold: str | None = None


def c(
    number: int,
    category: str,
    requirement: str,
    bug: str,
    gold: str,
    public: tuple[str, ...],
    hidden: tuple[str, ...],
    extra_bug: str | None = None,
    extra_gold: str | None = None,
) -> Case:
    prefix = {"localized": "localized", "cross_file": "cross-file", "diagnosis": "diagnosis", "adversarial": "adversarial"}[category]
    return Case(f"confirm-{prefix}-{number:03d}", category, requirement, bug, gold, public, hidden, extra_bug, extra_gold)


CASES = (
    c(1, "localized", "clamp(value, low, high) returns value inside the inclusive bounds and rejects low > high.",
      "def clamp(value, low, high):\n    if low > high:\n        raise ValueError('invalid bounds')\n    return max(low, min(value, high - 1))\n",
      "def clamp(value, low, high):\n    if low > high:\n        raise ValueError('invalid bounds')\n    return max(low, min(value, high))\n",
      ("self.assertEqual(clamp(10, 1, 10), 10)",), ("self.assertEqual(clamp(-4, -2, 3), -2)", "with self.assertRaises(ValueError): clamp(2, 4, 3)")),
    c(2, "localized", "chunks(items, size) returns every item in ordered chunks, including a short final chunk; size must be positive.",
      "def chunks(items, size):\n    if size <= 0:\n        raise ValueError('size')\n    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]\n",
      "def chunks(items, size):\n    if size <= 0:\n        raise ValueError('size')\n    return [items[i:i + size] for i in range(0, len(items), size)]\n",
      ("self.assertEqual(chunks([1, 2, 3], 2), [[1, 2], [3]])",), ("self.assertEqual(chunks([], 3), [])", "with self.assertRaises(ValueError): chunks([1], 0)")),
    c(3, "localized", "parse_bool accepts true/false, yes/no, and 1/0 case-insensitively after trimming; other values raise ValueError.",
      "def parse_bool(text):\n    value = text.strip().lower()\n    if value in {'true', 'yes'}:\n        return True\n    if value in {'false', 'no'}:\n        return False\n    raise ValueError('boolean')\n",
      "def parse_bool(text):\n    value = text.strip().lower()\n    if value in {'true', 'yes', '1'}:\n        return True\n    if value in {'false', 'no', '0'}:\n        return False\n    raise ValueError('boolean')\n",
      ("self.assertTrue(parse_bool(' 1 '))",), ("self.assertFalse(parse_bool('0'))", "with self.assertRaises(ValueError): parse_bool('maybe')")),
    c(4, "localized", "merge_intervals merges overlapping or directly adjacent inclusive integer intervals and returns sorted pairs.",
      "def merge_intervals(intervals):\n    result = []\n    for start, end in sorted(intervals):\n        if result and start <= result[-1][1]:\n            result[-1] = (result[-1][0], max(end, result[-1][1]))\n        else:\n            result.append((start, end))\n    return result\n",
      "def merge_intervals(intervals):\n    result = []\n    for start, end in sorted(intervals):\n        if result and start <= result[-1][1] + 1:\n            result[-1] = (result[-1][0], max(end, result[-1][1]))\n        else:\n            result.append((start, end))\n    return result\n",
      ("self.assertEqual(merge_intervals([(1, 2), (3, 5)]), [(1, 5)])",), ("self.assertEqual(merge_intervals([(8, 9), (1, 2), (2, 4)]), [(1, 4), (8, 9)])",)),
    c(5, "localized", "nearest_rank(values, percentile) uses the nearest-rank definition for percentile in (0, 100] and rejects empty input.",
      "import math\n\ndef nearest_rank(values, percentile):\n    ordered = sorted(values)\n    if not ordered or not 0 < percentile <= 100:\n        raise ValueError('input')\n    return ordered[max(0, math.floor(percentile / 100 * len(ordered)) - 1)]\n",
      "import math\n\ndef nearest_rank(values, percentile):\n    ordered = sorted(values)\n    if not ordered or not 0 < percentile <= 100:\n        raise ValueError('input')\n    return ordered[math.ceil(percentile / 100 * len(ordered)) - 1]\n",
      ("self.assertEqual(nearest_rank([1, 2, 3, 4], 51), 3)",), ("self.assertEqual(nearest_rank([4, 1, 2, 3], 100), 4)", "with self.assertRaises(ValueError): nearest_rank([], 50)")),
    c(6, "localized", "duration_ms parses integer strings ending in ms or s into milliseconds and rejects negative or malformed values.",
      "def duration_ms(text):\n    value = text.strip().lower()\n    if value.endswith('ms'):\n        return int(value[:-2]) * 1000\n    if value.endswith('s'):\n        return int(value[:-1]) * 1000\n    raise ValueError('duration')\n",
      "def duration_ms(text):\n    value = text.strip().lower()\n    factor, number = (1, value[:-2]) if value.endswith('ms') else ((1000, value[:-1]) if value.endswith('s') else (None, None))\n    if factor is None or not number.isdigit():\n        raise ValueError('duration')\n    return int(number) * factor\n",
      ("self.assertEqual(duration_ms('250ms'), 250)",), ("self.assertEqual(duration_ms(' 3s '), 3000)", "with self.assertRaises(ValueError): duration_ms('-1s')")),
    c(7, "localized", "retry_delay(attempt, base, cap) returns min(cap, base * 2**attempt) and rejects negative arguments.",
      "def retry_delay(attempt, base, cap):\n    if min(attempt, base, cap) < 0:\n        raise ValueError('negative')\n    return max(cap, base * (2 ** attempt))\n",
      "def retry_delay(attempt, base, cap):\n    if min(attempt, base, cap) < 0:\n        raise ValueError('negative')\n    return min(cap, base * (2 ** attempt))\n",
      ("self.assertEqual(retry_delay(1, 2, 10), 4)",), ("self.assertEqual(retry_delay(9, 2, 10), 10)", "with self.assertRaises(ValueError): retry_delay(-1, 2, 10)")),
    c(8, "localized", "slugify trims, lowercases, and joins every non-empty whitespace-delimited word with one hyphen.",
      "def slugify(text):\n    return '-'.join(text.strip().lower().split(' '))\n",
      "def slugify(text):\n    return '-'.join(text.strip().lower().split())\n",
      ("self.assertEqual(slugify('Alpha  Beta'), 'alpha-beta')",), ("self.assertEqual(slugify(' A\\tB\\nC '), 'a-b-c')",)),
    c(9, "localized", "csv_field quotes fields containing comma, quote, CR, or LF and doubles every embedded quote.",
      "def csv_field(value):\n    if any(mark in value for mark in [',', '\"', '\\r', '\\n']):\n        return '\"' + value.replace('\"', '\"\"', 1) + '\"'\n    return value\n",
      "def csv_field(value):\n    if any(mark in value for mark in [',', '\"', '\\r', '\\n']):\n        return '\"' + value.replace('\"', '\"\"') + '\"'\n    return value\n",
      ("self.assertEqual(csv_field('a\"b\"c'), '\"a\"\"b\"\"c\"')",), ("self.assertEqual(csv_field('plain'), 'plain')", "self.assertEqual(csv_field('a,b'), '\"a,b\"')")),
    c(10, "localized", "stable_unique returns the first occurrence of each hashable value while preserving input order.",
      "def stable_unique(values):\n    return list(dict.fromkeys(reversed(values)))[::-1]\n",
      "def stable_unique(values):\n    return list(dict.fromkeys(values))\n",
      ("self.assertEqual(stable_unique(['a', 'b', 'a']), ['a', 'b'])",), ("self.assertEqual(stable_unique([3, 2, 3, 1]), [3, 2, 1])",)),
    c(11, "localized", "window_sums(values, size) returns sums for all complete contiguous windows and rejects non-positive size.",
      "def window_sums(values, size):\n    if size <= 0:\n        raise ValueError('size')\n    return [sum(values[i:i + size]) for i in range(len(values) - size)]\n",
      "def window_sums(values, size):\n    if size <= 0:\n        raise ValueError('size')\n    return [sum(values[i:i + size]) for i in range(len(values) - size + 1)]\n",
      ("self.assertEqual(window_sums([1, 2, 3], 2), [3, 5])",), ("self.assertEqual(window_sums([4], 1), [4])",)),
    c(12, "localized", "valid_port(value) accepts only integers from 1 through 65535; booleans are not ports.",
      "def valid_port(value):\n    return isinstance(value, int) and 0 <= value <= 65536\n",
      "def valid_port(value):\n    return type(value) is int and 1 <= value <= 65535\n",
      ("self.assertFalse(valid_port(65536))",), ("self.assertTrue(valid_port(443))", "self.assertFalse(valid_port(True))", "self.assertFalse(valid_port(0))")),

    c(1, "diagnosis", "first_error_code scans noisy lines and returns the code from the first 'ERROR [CODE]' line, or None.",
      "import re\n\ndef first_error_code(lines):\n    found = re.findall(r'ERROR \\[([^]]+)\\]', '\\n'.join(lines))\n    return found[-1] if found else None\n",
      "import re\n\ndef first_error_code(lines):\n    found = re.search(r'ERROR \\[([^]]+)\\]', '\\n'.join(lines))\n    return found.group(1) if found else None\n",
      ("self.assertEqual(first_error_code(['ERROR [E1] x', 'ERROR [E2] y']), 'E1')",), ("self.assertIsNone(first_error_code(['INFO ok']))",)),
    c(2, "diagnosis", "root_cause returns the first non-empty line after 'Caused by:' while ignoring wrapper lines, or None.",
      "def root_cause(lines):\n    for line in lines:\n        if line.strip():\n            return line.strip()\n    return None\n",
      "def root_cause(lines):\n    for index, line in enumerate(lines):\n        if line.strip() == 'Caused by:':\n            for cause in lines[index + 1:]:\n                if cause.strip():\n                    return cause.strip()\n    return None\n",
      ("self.assertEqual(root_cause(['WrapperError', 'Caused by:', ' disk full ']), 'disk full')",), ("self.assertIsNone(root_cause(['WrapperError']))",)),
    c(3, "diagnosis", "failed_steps parses 'STEP name STATUS' lines and returns names whose status is exactly FAILED, preserving order.",
      "def failed_steps(lines):\n    return [line.split()[1] for line in lines if line.startswith('STEP') and line.split()[-1] != 'OK']\n",
      "def failed_steps(lines):\n    result = []\n    for line in lines:\n        parts = line.split()\n        if len(parts) == 3 and parts[0] == 'STEP' and parts[2] == 'FAILED':\n            result.append(parts[1])\n    return result\n",
      ("self.assertEqual(failed_steps(['STEP build SKIPPED', 'STEP test FAILED']), ['test'])",), ("self.assertEqual(failed_steps(['noise', 'STEP a FAILED', 'STEP b OK']), ['a'])",)),
    c(4, "diagnosis", "classify_timeout returns 'timeout' when a message contains timeout or timed out case-insensitively, else 'other'.",
      "def classify_timeout(message):\n    return 'timeout' if 'timeout' in message else 'other'\n",
      "def classify_timeout(message):\n    text = message.casefold()\n    return 'timeout' if 'timeout' in text or 'timed out' in text else 'other'\n",
      ("self.assertEqual(classify_timeout('Request TIMED OUT'), 'timeout')",), ("self.assertEqual(classify_timeout('deadline exceeded'), 'other')",)),
    c(5, "diagnosis", "extract_timestamp returns the ISO-like token immediately following 'at=' in a noisy line, without trailing punctuation.",
      "def extract_timestamp(line):\n    return line.split('at=', 1)[1] if 'at=' in line else None\n",
      "import re\n\ndef extract_timestamp(line):\n    match = re.search(r'(?:^|\\s)at=([0-9T:+-]+)', line)\n    return match.group(1) if match else None\n",
      ("self.assertEqual(extract_timestamp('INFO at=2026-07-15T01:02:03+00:00, retry'), '2026-07-15T01:02:03+00:00')",), ("self.assertIsNone(extract_timestamp('INFO no timestamp'))",)),
    c(6, "diagnosis", "last_retryable_status returns the last integer status in {408, 429, 500, 502, 503, 504} found in noisy lines, or None.",
      "import re\n\ndef last_retryable_status(lines):\n    for line in lines:\n        for value in re.findall(r'\\b\\d{3}\\b', line):\n            if int(value) in {408, 429, 500, 502, 503, 504}:\n                return int(value)\n    return None\n",
      "import re\n\ndef last_retryable_status(lines):\n    result = None\n    for line in lines:\n        for value in re.findall(r'\\b\\d{3}\\b', line):\n            if int(value) in {408, 429, 500, 502, 503, 504}:\n                result = int(value)\n    return result\n",
      ("self.assertEqual(last_retryable_status(['got 429', 'then 503']), 503)",), ("self.assertIsNone(last_retryable_status(['200 ok', '404 no']))",)),

    c(1, "adversarial", "canonical_words trims, casefolds, and joins every Unicode-whitespace-delimited word with one hyphen.",
      "def canonical_words(text):\n    return '-'.join(text.strip().casefold().split(' '))\n",
      "def canonical_words(text):\n    return '-'.join(text.strip().casefold().split())\n",
      ("self.assertEqual(canonical_words('A  B'), 'a-b')",), ("self.assertEqual(canonical_words(' A\\tB\\u2003C '), 'a-b-c')",)),
    c(2, "adversarial", "safe_relative(parts) joins path parts but rejects absolute paths and any traversal segment equal to '..'.",
      "def safe_relative(parts):\n    value = '/'.join(parts).replace('../', '')\n    if value.startswith('/'):\n        raise ValueError('absolute')\n    return value\n",
      "def safe_relative(parts):\n    if not parts or any(part == '..' for part in parts) or any(part.startswith('/') for part in parts):\n        raise ValueError('unsafe path')\n    return '/'.join(part for part in parts if part not in {'', '.'})\n",
      ("with self.assertRaises(ValueError): safe_relative(['..', 'secret'])",), ("with self.assertRaises(ValueError): safe_relative(['safe', '..', 'secret'])", "self.assertEqual(safe_relative(['a', '.', 'b']), 'a/b')")),
    c(3, "adversarial", "typed_unique preserves first occurrences and treats values of different exact Python types as distinct, including True and 1.",
      "def typed_unique(values):\n    return list(dict.fromkeys(values))\n",
      "def typed_unique(values):\n    seen = set()\n    result = []\n    for value in values:\n        key = (type(value), value)\n        if key not in seen:\n            seen.add(key)\n            result.append(value)\n    return result\n",
      ("self.assertEqual(typed_unique([1, 2, 1]), [1, 2])", "self.assertEqual(len(typed_unique([True, 1])), 2)"), ("self.assertEqual(typed_unique([1, 1.0, False, 0]), [1, 1.0, False, 0])",)),
    c(4, "adversarial", "redact_tokens replaces every case-insensitive 'token=<non-space>' value with 'token=[REDACTED]'.",
      "import re\n\ndef redact_tokens(text):\n    return re.sub(r'token=\\S+', 'token=[REDACTED]', text, count=1, flags=re.I)\n",
      "import re\n\ndef redact_tokens(text):\n    return re.sub(r'token=\\S+', 'token=[REDACTED]', text, flags=re.I)\n",
      ("self.assertEqual(redact_tokens('token=a token=b'), 'token=[REDACTED] token=[REDACTED]')",), ("self.assertEqual(redact_tokens('TOKEN=abc'), 'token=[REDACTED]')",)),

    c(1, "cross_file", "quote_total(subtotal, member) applies policy.discount_rate then rounds the final amount to two decimals.",
      "from policy import discount_rate\n\ndef quote_total(subtotal, member):\n    return subtotal * (1 - discount_rate(subtotal, member))\n",
      "from policy import discount_rate\n\ndef quote_total(subtotal, member):\n    return round(subtotal * (1 - discount_rate(subtotal, member)), 2)\n",
      ("self.assertEqual(quote_total(100.05, True), 90.05)",), ("self.assertEqual(quote_total(50, False), 50.0)",),
      "def discount_rate(subtotal, member):\n    return 0.1 if member and subtotal > 100 else 0.0\n",
      "def discount_rate(subtotal, member):\n    return 0.1 if member and subtotal >= 100 else 0.0\n"),
    c(2, "cross_file", "reserve(state, sku, quantity) validates quantity through inventory and returns a new state with stock reduced exactly once.",
      "from inventory import can_reserve\n\ndef reserve(state, sku, quantity):\n    if not can_reserve(state, sku, quantity):\n        raise ValueError('stock')\n    result = dict(state)\n    result[sku] -= quantity + 1\n    return result\n",
      "from inventory import can_reserve\n\ndef reserve(state, sku, quantity):\n    if not can_reserve(state, sku, quantity):\n        raise ValueError('stock')\n    result = dict(state)\n    result[sku] -= quantity\n    return result\n",
      ("self.assertEqual(reserve({'a': 3}, 'a', 2), {'a': 1})",), ("with self.assertRaises(ValueError): reserve({'a': 3}, 'a', 0)",),
      "def can_reserve(state, sku, quantity):\n    return quantity >= 0 and state.get(sku, 0) >= quantity\n",
      "def can_reserve(state, sku, quantity):\n    return quantity > 0 and state.get(sku, 0) >= quantity\n"),
    c(3, "cross_file", "page_meta(total, page, size) validates positive page/size and returns page, size, and ceiling total_pages.",
      "from paging import total_pages\n\ndef page_meta(total, page, size):\n    if page < 0 or size < 0:\n        raise ValueError('paging')\n    return {'page': page, 'size': size, 'total_pages': total_pages(total, size)}\n",
      "from paging import total_pages\n\ndef page_meta(total, page, size):\n    if page <= 0 or size <= 0 or total < 0:\n        raise ValueError('paging')\n    return {'page': page, 'size': size, 'total_pages': total_pages(total, size)}\n",
      ("self.assertEqual(page_meta(11, 1, 5)['total_pages'], 3)",), ("with self.assertRaises(ValueError): page_meta(1, 0, 5)",),
      "def total_pages(total, size):\n    return total // size\n",
      "def total_pages(total, size):\n    return (total + size - 1) // size\n"),
    c(4, "cross_file", "is_admin(headers) uses normalized_roles and returns true only for an exact case-insensitive admin role.",
      "from roles import normalized_roles\n\ndef is_admin(headers):\n    return any('admin' in role for role in normalized_roles(headers.get('roles', '')))\n",
      "from roles import normalized_roles\n\ndef is_admin(headers):\n    return 'admin' in normalized_roles(headers.get('roles', ''))\n",
      ("self.assertFalse(is_admin({'roles': 'superadmin,user'}))",), ("self.assertTrue(is_admin({'roles': ' USER, Admin '}))",),
      "def normalized_roles(value):\n    return value.lower().split(',')\n",
      "def normalized_roles(value):\n    return {part.strip().casefold() for part in value.split(',') if part.strip()}\n"),
    c(5, "cross_file", "available_slots(existing, candidates) returns sorted candidate intervals that do not overlap any existing half-open interval.",
      "from intervals import overlaps\n\ndef available_slots(existing, candidates):\n    return [slot for slot in candidates if not any(overlaps(slot, busy) for busy in existing)]\n",
      "from intervals import overlaps\n\ndef available_slots(existing, candidates):\n    return sorted(slot for slot in candidates if not any(overlaps(slot, busy) for busy in existing))\n",
      ("self.assertEqual(available_slots([(2, 4)], [(4, 5), (1, 2)]), [(1, 2), (4, 5)])",), ("self.assertEqual(available_slots([(2, 4)], [(3, 5)]), [])",),
      "def overlaps(left, right):\n    return left[0] <= right[1] and right[0] <= left[1]\n",
      "def overlaps(left, right):\n    return left[0] < right[1] and right[0] < left[1]\n"),
    c(6, "cross_file", "get_setting(lines, key, default) parses trimmed key=value pairs, ignores comments, and returns the last matching value or default.",
      "from config_parse import parse_lines\n\ndef get_setting(lines, key, default=None):\n    return parse_lines(lines).get(key)\n",
      "from config_parse import parse_lines\n\ndef get_setting(lines, key, default=None):\n    return parse_lines(lines).get(key, default)\n",
      ("self.assertEqual(get_setting([' x = 1 '], 'x'), '1')",), ("self.assertEqual(get_setting(['# x=1'], 'x', 'd'), 'd')",),
      "def parse_lines(lines):\n    return dict(line.split('=', 1) for line in lines if '=' in line)\n",
      "def parse_lines(lines):\n    result = {}\n    for line in lines:\n        line = line.strip()\n        if line and not line.startswith('#') and '=' in line:\n            key, value = line.split('=', 1)\n            result[key.strip()] = value.strip()\n    return result\n"),
    c(7, "cross_file", "accepted_payloads(events) decodes versioned events, skips unknown event types, and preserves accepted payload order.",
      "from event_codec import decode\n\ndef accepted_payloads(events):\n    return [decode(event)['payload'] for event in events]\n",
      "from event_codec import decode\n\ndef accepted_payloads(events):\n    result = []\n    for event in events:\n        decoded = decode(event)\n        if decoded is not None:\n            result.append(decoded['payload'])\n    return result\n",
      ("self.assertEqual(accepted_payloads(['v1|DATA|a', 'v1|OTHER|b']), ['a'])",), ("self.assertEqual(accepted_payloads([]), [])",),
      "def decode(value):\n    version, kind, payload = value.split('|', 2)\n    return {'version': version, 'kind': kind, 'payload': payload}\n",
      "def decode(value):\n    parts = value.split('|', 2)\n    if len(parts) != 3 or parts[0] != 'v1' or parts[1] != 'DATA':\n        return None\n    return {'version': parts[0], 'kind': parts[1], 'payload': parts[2]}\n"),
    c(8, "cross_file", "transfer(balances, source, target, amount) validates debit policy and returns a new mapping with conserved total balance.",
      "from ledger import valid_debit\n\ndef transfer(balances, source, target, amount):\n    if not valid_debit(balances.get(source, 0), amount):\n        raise ValueError('debit')\n    result = dict(balances)\n    result[source] -= amount\n    result[target] = result.get(target, 0) + amount + 1\n    return result\n",
      "from ledger import valid_debit\n\ndef transfer(balances, source, target, amount):\n    if source == target or not valid_debit(balances.get(source, 0), amount):\n        raise ValueError('debit')\n    result = dict(balances)\n    result[source] -= amount\n    result[target] = result.get(target, 0) + amount\n    return result\n",
      ("self.assertEqual(transfer({'a': 5, 'b': 1}, 'a', 'b', 2), {'a': 3, 'b': 3})",), ("with self.assertRaises(ValueError): transfer({'a': 5}, 'a', 'a', 2)",),
      "def valid_debit(balance, amount):\n    return amount >= 0 and balance >= amount\n",
      "def valid_debit(balance, amount):\n    return amount > 0 and balance >= amount\n"),
)


def _test_file(case: Case, assertions: tuple[str, ...]) -> str:
    module = "service" if case.extra_bug is not None else "solution"
    methods = []
    for index, assertion in enumerate(assertions, 1):
        body = "\n".join(f"        {line}" for line in assertion.splitlines())
        methods.append(f"    def test_case_{index}(self):\n{body}\n")
    return (
        "import sys\nimport unittest\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
        f"from {module} import *\n\n"
        "class BehaviorTests(unittest.TestCase):\n"
        + "\n".join(methods)
        + "\nif __name__ == '__main__':\n    unittest.main()\n"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(case: Case) -> None:
    task_dir = TASK_ROOT / case.id
    evaluator_dir = EVALUATOR_ROOT / case.id
    public = task_dir / "public"
    _write(public / "README.md", f"# Repair task: {case.id}\n\n{case.requirement}\n\nFix `src/`; do not modify tests.\n")
    _write(public / "LICENSE", LICENSE)
    _write(public / "requirements.lock", "# Python standard library only.\n")
    _write(public / "src" / ("service.py" if case.extra_bug else "solution.py"), case.bug)
    if case.extra_bug is not None:
        _write(public / "src" / case.gold.split("from ", 1)[1].split(" import", 1)[0].strip().replace(".", "/") .__add__(".py"), case.extra_bug)
    _write(public / "tests/public/test_behavior.py", _test_file(case, case.public))
    _write(task_dir / "provenance.md", "Generated seeded mutation created for EdgeLoopBench v0.2; MIT licensed; no network data.\n")
    _write(evaluator_dir / "tests/test_hidden.py", _test_file(case, case.hidden))

    with tempfile.TemporaryDirectory() as directory:
        worktree = Path(directory) / "worktree"
        shutil.copytree(public, worktree)
        _run_git(worktree, ["init", "-q", "--initial-branch=main"])
        _run_git(worktree, ["add", "--all"])
        _run_git(worktree, ["commit", "-q", "-m", f"Initialize {case.id}"])
        commit = _run_git(worktree, ["rev-parse", "HEAD"]).strip()
        _write(worktree / "src" / ("service.py" if case.extra_bug else "solution.py"), case.gold)
        if case.extra_gold is not None:
            helper = case.gold.split("from ", 1)[1].split(" import", 1)[0].strip().replace(".", "/") + ".py"
            _write(worktree / "src" / helper, case.extra_gold)
        patch = subprocess.run(
            ["git", "diff", "--binary", "--unified=0", "--", "src"], cwd=worktree,
            check=True, capture_output=True, text=True,
        ).stdout
    _write(evaluator_dir / "gold.patch", patch)
    patch_sha = hashlib.sha256(patch.encode()).hexdigest()
    manifest_category = "cross-file" if case.category == "cross_file" else case.category
    manifest = f'''schema_version = 1
id = "{case.id}"
language = "python"
category = "{manifest_category}"
source_type = "generated_mutation"
license = "MIT"
initial_commit = "{commit}"
gold_patch_sha256 = "sha256:{patch_sha}"

allowed_paths = ["src/**"]
prohibited_paths = ["tests/**", ".edgeloop/**"]

[public_test]
command = ["python3", "-m", "unittest", "discover", "-s", "tests/public"]
timeout_seconds = 30

[hidden_evaluation]
evaluator_id = "external-{case.id}"
timeout_seconds = 30
'''
    _write(task_dir / "task.toml", manifest)


def main() -> None:
    shutil.rmtree(TASK_ROOT, ignore_errors=True)
    shutil.rmtree(EVALUATOR_ROOT, ignore_errors=True)
    for case in CASES:
        build(case)
    rows = [
        f"| `{case.id}` | {case.category.replace('_', '-')} | {case.requirement} |"
        for case in CASES
    ]
    _write(
        TASK_ROOT / "README.md",
        "# ConfirmatoryRepair-30\n\n"
        "Frozen offline seeded-mutation suite for the v0.2 confirmatory experiment. "
        "These tasks are disjoint from MicroRepair-6 calibration. Each model sees only "
        "the public bundle; hidden tests and gold patches remain under the evaluator root.\n\n"
        "Composition: 12 localized, 8 cross-file, 6 diagnosis, and 4 adversarial tasks. "
        "All tasks use the Python standard library and MIT-licensed generated source.\n\n"
        "| Task | Category | Exact required behavior |\n"
        "| --- | --- | --- |\n"
        + "\n".join(rows)
        + "\n",
    )
    print(f"generated {len(CASES)} confirmatory tasks")


if __name__ == "__main__":
    main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(extract_timestamp('INFO at=2026-07-15T01:02:03+00:00, retry'), '2026-07-15T01:02:03+00:00')

if __name__ == '__main__':
    unittest.main()

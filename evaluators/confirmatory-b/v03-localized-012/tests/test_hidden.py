import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(normalize_address(' User@Example.COM '), 'user@example.com')

    def test_case_2(self):
        with self.assertRaises(ValueError): normalize_address('missing')

    def test_case_3(self):
        with self.assertRaises(ValueError): normalize_address('@host')

if __name__ == '__main__':
    unittest.main()

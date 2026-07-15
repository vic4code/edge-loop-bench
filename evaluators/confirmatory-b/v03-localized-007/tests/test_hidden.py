import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(normalize_percent(0), 0.0)

    def test_case_2(self):
        with self.assertRaises(ValueError): normalize_percent(-1)

    def test_case_3(self):
        with self.assertRaises(ValueError): normalize_percent(True)

if __name__ == '__main__':
    unittest.main()

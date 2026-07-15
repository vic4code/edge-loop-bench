import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(numeric_median([3, 1, 2]), 2.0)

    def test_case_2(self):
        with self.assertRaises(ValueError): numeric_median([])

    def test_case_3(self):
        with self.assertRaises(ValueError): numeric_median([True, 2])

if __name__ == '__main__':
    unittest.main()

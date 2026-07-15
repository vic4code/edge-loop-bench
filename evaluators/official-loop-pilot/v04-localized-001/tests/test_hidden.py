import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(page_count(0, 5), 0)

    def test_case_2(self):
        with self.assertRaises(ValueError): page_count(-1, 5)

    def test_case_3(self):
        with self.assertRaises(ValueError): page_count(1, 0)

if __name__ == '__main__':
    unittest.main()

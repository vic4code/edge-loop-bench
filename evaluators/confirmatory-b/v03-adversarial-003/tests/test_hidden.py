import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(merge_ranges([(5, 7), (1, 2)]), [(1, 2), (5, 7)])

    def test_case_2(self):
        with self.assertRaises(ValueError): merge_ranges([(3, 2)])

if __name__ == '__main__':
    unittest.main()

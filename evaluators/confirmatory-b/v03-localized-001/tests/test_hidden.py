import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(cap_inclusive(-3, -1, 8), -1)

    def test_case_2(self):
        with self.assertRaises(ValueError): cap_inclusive(0, 2, 1)

if __name__ == '__main__':
    unittest.main()

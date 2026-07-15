import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(clamp_ratio(-2), 0.0)

    def test_case_2(self):
        with self.assertRaises(ValueError): clamp_ratio(True)

    def test_case_3(self):
        with self.assertRaises(ValueError): clamp_ratio(float('inf'))

if __name__ == '__main__':
    unittest.main()

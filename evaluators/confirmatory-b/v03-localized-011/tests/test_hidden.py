import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(safe_average([2, None, 4]), 3.0)

    def test_case_2(self):
        with self.assertRaises(ValueError): safe_average([True, 2])

if __name__ == '__main__':
    unittest.main()

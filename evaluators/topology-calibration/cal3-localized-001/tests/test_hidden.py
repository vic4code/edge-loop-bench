import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(bounded_score(-2, 8), 0)

    def test_case_2(self):
        with self.assertRaises(ValueError): bounded_score(1, -1)

if __name__ == '__main__':
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(nearest_rank([4, 1, 2, 3], 100), 4)

    def test_case_2(self):
        with self.assertRaises(ValueError): nearest_rank([], 50)

if __name__ == '__main__':
    unittest.main()

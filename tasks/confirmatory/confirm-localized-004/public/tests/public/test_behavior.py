import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(merge_intervals([(1, 2), (3, 5)]), [(1, 5)])

if __name__ == '__main__':
    unittest.main()

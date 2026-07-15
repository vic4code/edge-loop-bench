import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(stable_unique([[1], [1], [2]]), [[1], [2]])

    def test_case_2(self):
        self.assertEqual(stable_unique([]), [])

if __name__ == '__main__':
    unittest.main()

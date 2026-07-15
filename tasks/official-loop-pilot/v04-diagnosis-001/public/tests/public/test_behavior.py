import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(active_total([None, {'active': True, 'amount': 4}, {'active': False, 'amount': 9}]), 4)

if __name__ == '__main__':
    unittest.main()

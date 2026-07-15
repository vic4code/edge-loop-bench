import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(active_total([{'active': True, 'amount': True}, {'active': True, 'amount': 2.5}, {}]), 2.5)

if __name__ == '__main__':
    unittest.main()

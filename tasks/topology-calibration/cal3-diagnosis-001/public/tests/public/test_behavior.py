import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(accepted_total([None, {'status': 'accepted', 'amount': 4}, {'status': 'rejected', 'amount': 9}]), 4)

if __name__ == '__main__':
    unittest.main()

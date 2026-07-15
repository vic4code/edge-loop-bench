import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(display_name({}, 1), 'UNKNOWN')

    def test_case_2(self):
        self.assertEqual(display_name({1: None}, 1), 'UNKNOWN')

if __name__ == '__main__':
    unittest.main()

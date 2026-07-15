import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(retry_delay(2, 0, 30), 2)

    def test_case_2(self):
        self.assertEqual(retry_delay(2, 3, 10), 10)

if __name__ == '__main__':
    unittest.main()

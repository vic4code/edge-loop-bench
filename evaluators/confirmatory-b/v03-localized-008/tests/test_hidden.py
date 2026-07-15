import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(retry_delay(1.5, 2, 20), 6.0)

    def test_case_2(self):
        with self.assertRaises(ValueError): retry_delay(1, -1, 3)

if __name__ == '__main__':
    unittest.main()

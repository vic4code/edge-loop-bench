import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(duration_ms(' 3s '), 3000)

    def test_case_2(self):
        with self.assertRaises(ValueError): duration_ms('-1s')

if __name__ == '__main__':
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(chunks([], 3), [])

    def test_case_2(self):
        with self.assertRaises(ValueError): chunks([1], 0)

if __name__ == '__main__':
    unittest.main()

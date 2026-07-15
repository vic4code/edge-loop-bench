import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(canonical_path(['a', None, ' b ']), 'a/b')

    def test_case_2(self):
        with self.assertRaises(ValueError): canonical_path([' ', ''])

if __name__ == '__main__':
    unittest.main()

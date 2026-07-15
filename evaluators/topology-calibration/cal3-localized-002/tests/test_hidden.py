import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(compact_code(' A\tB\nC '), 'a_b_c')

    def test_case_2(self):
        with self.assertRaises(ValueError): compact_code('  ')

if __name__ == '__main__':
    unittest.main()

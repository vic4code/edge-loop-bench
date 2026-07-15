import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(final_price(12.34, 'standard'), 12.34)

    def test_case_2(self):
        with self.assertRaises(ValueError): final_price(-1, 'member')

if __name__ == '__main__':
    unittest.main()

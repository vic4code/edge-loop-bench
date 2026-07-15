import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(taxed_total(12.34, 'north'), 12.96)

    def test_case_2(self):
        with self.assertRaises(ValueError): taxed_total(-1, 'north')

if __name__ == '__main__':
    unittest.main()

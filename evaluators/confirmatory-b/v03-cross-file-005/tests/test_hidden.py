import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(delivery_cost(1.25, True), 4.38)

    def test_case_2(self):
        with self.assertRaises(ValueError): delivery_cost(-1, False)

if __name__ == '__main__':
    unittest.main()

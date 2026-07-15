import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        state = {'x': {'available': 2, 'reserved': 1}}
        self.assertTrue(reserve_units(state, 'x', 2))
        self.assertEqual(state['x'], {'available': 0, 'reserved': 3})

    def test_case_2(self):
        state = {'x': {'available': 2, 'reserved': 0}}
        self.assertFalse(reserve_units(state, 'x', 0))
        self.assertEqual(state['x']['available'], 2)

if __name__ == '__main__':
    unittest.main()

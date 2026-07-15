import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(group_values([None, {'group': ' a ', 'value': 1}, {'group': 'a', 'value': 2}]), {'a': [1, 2]})

if __name__ == '__main__':
    unittest.main()

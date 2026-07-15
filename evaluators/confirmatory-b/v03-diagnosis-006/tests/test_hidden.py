import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(group_values([None, {'group': ' ', 'value': 1}, {'group': ' b ', 'value': 3}, {'group': 'b'}]), {'b': [3]})

if __name__ == '__main__':
    unittest.main()

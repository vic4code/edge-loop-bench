import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(transfer({'a': 5, 'b': 1}, 'a', 'b', 2), {'a': 3, 'b': 3})

if __name__ == '__main__':
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        with self.assertRaises(ValueError): reserve({'a': 3}, 'a', 0)

if __name__ == '__main__':
    unittest.main()

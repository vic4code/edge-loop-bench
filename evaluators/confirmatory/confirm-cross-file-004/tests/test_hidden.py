import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertTrue(is_admin({'roles': ' USER, Admin '}))

if __name__ == '__main__':
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(cache_read({'x': None}, 'x'), (True, None))

    def test_case_2(self):
        self.assertEqual(cache_read({}, 'x'), (False, None))

if __name__ == '__main__':
    unittest.main()

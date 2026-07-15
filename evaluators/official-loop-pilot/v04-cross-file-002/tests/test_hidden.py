import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(record_lookup({'x': None}, 'x'), (True, None))

    def test_case_2(self):
        self.assertEqual(record_lookup({}, 'x'), (False, None))

if __name__ == '__main__':
    unittest.main()

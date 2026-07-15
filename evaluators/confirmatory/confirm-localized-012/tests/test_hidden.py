import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertTrue(valid_port(443))

    def test_case_2(self):
        self.assertFalse(valid_port(True))

    def test_case_3(self):
        self.assertFalse(valid_port(0))

if __name__ == '__main__':
    unittest.main()

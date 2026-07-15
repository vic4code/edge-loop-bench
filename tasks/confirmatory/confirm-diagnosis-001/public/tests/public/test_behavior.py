import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(first_error_code(['ERROR [E1] x', 'ERROR [E2] y']), 'E1')

if __name__ == '__main__':
    unittest.main()

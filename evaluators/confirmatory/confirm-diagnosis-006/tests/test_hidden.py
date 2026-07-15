import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertIsNone(last_retryable_status(['200 ok', '404 no']))

if __name__ == '__main__':
    unittest.main()

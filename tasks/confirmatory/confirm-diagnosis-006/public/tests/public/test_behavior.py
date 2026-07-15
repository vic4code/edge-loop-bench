import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(last_retryable_status(['got 429', 'then 503']), 503)

if __name__ == '__main__':
    unittest.main()

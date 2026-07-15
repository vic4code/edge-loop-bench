import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(latest_by_key([{'key': 'x', 'version': 3}, {'key': 'x', 'version': 1}], 'x')['version'], 3)

if __name__ == '__main__':
    unittest.main()

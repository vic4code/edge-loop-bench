import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(page_meta(11, 1, 5)['total_pages'], 3)

if __name__ == '__main__':
    unittest.main()

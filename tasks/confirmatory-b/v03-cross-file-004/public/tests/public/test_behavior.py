import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertFalse(can_publish({'roles': []}, {'status': 'draft'}))

    def test_case_2(self):
        self.assertFalse(can_publish({'roles': ['editor']}, {'status': 'published'}))

if __name__ == '__main__':
    unittest.main()

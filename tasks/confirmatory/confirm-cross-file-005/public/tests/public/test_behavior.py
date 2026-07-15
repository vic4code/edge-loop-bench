import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(available_slots([(2, 4)], [(4, 5), (1, 2)]), [(1, 2), (4, 5)])

if __name__ == '__main__':
    unittest.main()

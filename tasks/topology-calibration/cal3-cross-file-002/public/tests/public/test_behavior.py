import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        store = {'api': 2}
        self.assertFalse(consume_quota(store, 'api', -1))
        self.assertEqual(store, {'api': 2})

if __name__ == '__main__':
    unittest.main()

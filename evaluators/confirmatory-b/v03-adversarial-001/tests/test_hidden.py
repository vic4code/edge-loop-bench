import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(label_key(' A\t  B\nC '), 'a-b-c')

    def test_case_2(self):
        with self.assertRaises(ValueError): label_key('\t ')

if __name__ == '__main__':
    unittest.main()

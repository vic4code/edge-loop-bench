import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from solution import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(file_extension('.env'), '')

    def test_case_2(self):
        self.assertEqual(file_extension('name.'), '')

    def test_case_3(self):
        self.assertEqual(file_extension('dir/a.TXT'), 'txt')

if __name__ == '__main__':
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'src'))
from service import *

class BehaviorTests(unittest.TestCase):
    def test_case_1(self):
        self.assertEqual(invoice_total(49.99, True), 49.99)

    def test_case_2(self):
        self.assertEqual(invoice_total(55.55, True), 51.11)

if __name__ == '__main__':
    unittest.main()

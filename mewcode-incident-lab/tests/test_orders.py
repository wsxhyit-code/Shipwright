import unittest
from app.orders import summarize


class ExistingTests(unittest.TestCase):
    def test_normal_customer(self):
        self.assertEqual(summarize([1000, 3000]), {"count": 2, "total_cents": 4000, "average_cents": 2000})

    def test_single_order(self):
        self.assertEqual(summarize([1500])["average_cents"], 1500)

    def test_zero_value_order(self):
        self.assertEqual(summarize([0])["average_cents"], 0)

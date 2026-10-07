"""Fixed evaluator: keep outside Agent edit permissions. Expected to fail before repair."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from app.orders import summarize
from app.service import make_server
from lab.init import initialize


class RegressionTests(unittest.TestCase):
    def test_empty_orders_contract(self):
        self.assertEqual(summarize([]), {"count": 0, "total_cents": 0, "average_cents": None})

    def test_http_empty_and_normal_customer(self):
        with tempfile.TemporaryDirectory() as folder:
            initialize(folder)
            server = make_server("127.0.0.1", 0, folder)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for customer, expected in [(1002, {"count": 0, "total_cents": 0, "average_cents": None}),
                                           (1001, {"count": 2, "total_cents": 4000, "average_cents": 2000})]:
                    try:
                        response = urlopen(f"http://127.0.0.1:{server.server_port}/api/orders/summary?customer_id={customer}", timeout=3)
                    except HTTPError as exc:
                        response = exc
                    with response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read()), expected)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_normal_results_unchanged(self):
        self.assertEqual(summarize([1000, 3000])["average_cents"], 2000)
        self.assertEqual(summarize([0])["count"], 1)

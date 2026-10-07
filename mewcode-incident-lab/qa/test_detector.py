import unittest
from lab.monitor import Detector, classify


def event(ts=100, status=500, code="APPLICATION_EXCEPTION", revision="old"):
    return {"ts": ts, "event": "http_request", "service": "order-service", "path": "/api/orders/summary",
            "status": status, "revision": revision, "error_code": code,
            "traceback": "stack", "database_exists": False}


class DetectorTests(unittest.TestCase):
    def test_small_sample_does_not_fire(self):
        detector = Detector(hold=0)
        for _ in range(5):
            detector.add(event())
        self.assertEqual(detector.evaluate(100), [])

    def test_hold_and_dedup(self):
        detector = Detector(hold=2)
        for _ in range(12):
            detector.add(event())
        self.assertEqual(detector.evaluate(100), [])
        alert = detector.evaluate(102)
        self.assertEqual(len(alert), 1)
        self.assertTrue(alert[0]["code_fix_candidate"])
        self.assertEqual(detector.evaluate(103), [])

    def test_expired_errors_do_not_fire(self):
        detector = Detector(hold=0)
        for _ in range(12):
            detector.add(event())
        self.assertEqual(detector.evaluate(161), [])

    def test_normal_and_client_errors_do_not_fire(self):
        detector = Detector(hold=0)
        for status in [200, 400] * 10:
            detector.add(event(status=status))
        self.assertEqual(detector.evaluate(100), [])

    def test_rate_threshold(self):
        detector = Detector(hold=0)
        for status in [500] * 5 + [200] * 95:
            detector.add(event(status=status))
        self.assertEqual(detector.evaluate(100), [])

    def test_classification_not_only_status_code(self):
        category, eligible, _ = classify([event(code="DATABASE_PATH_MISSING")])
        self.assertEqual(category, "configuration")
        self.assertFalse(eligible)
        category, eligible, _ = classify([event(code="UPSTREAM_UNAVAILABLE")])
        self.assertEqual(category, "dependency_or_network")
        self.assertFalse(eligible)
        category, eligible, _ = classify([event(code="UNRECOGNIZED")])
        self.assertEqual(category, "unknown")
        self.assertFalse(eligible)

    def test_mixed_evidence_is_unknown(self):
        category, eligible, _ = classify([event(), event(code="UPSTREAM_UNAVAILABLE")])
        self.assertEqual(category, "unknown")
        self.assertFalse(eligible)

    def test_no_traffic_is_not_recovery(self):
        detector = Detector(hold=0, recovery_hold=0)
        for _ in range(12):
            detector.add(event())
        detector.evaluate(100)
        self.assertEqual(detector.evaluate(161), [])
        self.assertEqual(len(detector.active), 1)

    def test_new_deployment_success_resolves_old_alert(self):
        detector = Detector(hold=0, recovery_hold=2)
        for _ in range(12):
            detector.add(event())
        detector.evaluate(100)
        for _ in range(12):
            detector.add(event(ts=162, status=200, revision="new"))
        self.assertEqual(detector.evaluate(162), [])
        notification = detector.evaluate(164)
        self.assertEqual(notification[0]["status"], "resolved")
        self.assertEqual(notification[0]["recovered_revision"], "new")

    def test_request_grouping(self):
        detector = Detector(hold=0)
        for _ in range(6):
            detector.add(event(revision="old"))
            detector.add(event(revision="new"))
        self.assertEqual(detector.evaluate(100), [])

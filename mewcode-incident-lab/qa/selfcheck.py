"""Exercise real HTTP failures and reference repair in a temporary copy only.

This is infrastructure QA, not an AI Agent run. It creates no real PR.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from app.service import make_server
from lab.init import initialize
from lab.monitor import Detector, persist_notifications


ROOT = Path(__file__).resolve().parents[1]


def get(server, path):
    try:
        response = urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def scenario(runtime, database, path):
    server = make_server("127.0.0.1", 0, runtime, database, "http://127.0.0.1:1/health")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        health = get(server, "/health")[0]
        responses = [get(server, path)[0] for _ in range(12)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    detector = Detector(hold=0)
    for line in (Path(runtime) / "service.jsonl").read_text(encoding="utf-8").splitlines():
        detector.add(json.loads(line))
    alerts = detector.evaluate(time.time())
    persist_notifications(alerts, runtime, ROOT)
    return {"health": health, "http_statuses": responses,
            "classification": alerts[0]["classification"], "code_fix_candidate": alerts[0]["code_fix_candidate"]}


def command(cwd, *args):
    result = subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=45)
    return {"exit_code": result.returncode, "output": result.stdout + result.stderr}


def main():
    report = {"scope": "real local HTTP + mechanical gates + QA reference repair", "agent_executed": False,
              "github_pr_created": False}
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        for name in ["code", "config", "dependency"]:
            initialize(folder / name)
        report["code"] = scenario(folder / "code", None, "/api/orders/summary?customer_id=1002")
        report["configuration"] = scenario(folder / "config", folder / "missing.sqlite3", "/api/orders/summary?customer_id=1001")
        report["dependency"] = scenario(folder / "dependency", None, "/api/shipping/quote")
        assert report["code"]["health"] == 200 and set(report["code"]["http_statuses"]) == {500}
        assert report["code"]["classification"] == "application_candidate"
        assert report["configuration"]["classification"] == "configuration"
        assert report["dependency"]["classification"] == "dependency_or_network"
        assert not report["configuration"]["code_fix_candidate"] and not report["dependency"]["code_fix_candidate"]
        report["baseline_existing_tests"] = command(ROOT, "-m", "unittest", "discover", "-s", "tests", "-v")
        report["before_repair_verification"] = command(ROOT, "-m", "lab.verify", "--runtime", str(folder / "before"))
        assert report["baseline_existing_tests"]["exit_code"] == 0
        assert report["before_repair_verification"]["exit_code"] != 0
        working = folder / "reference-repair"
        shutil.copytree(ROOT, working, ignore=shutil.ignore_patterns("runtime", "__pycache__", ".git"))
        target = working / "app" / "orders.py"
        source = target.read_text(encoding="utf-8")
        target.write_text(source.replace("sum(amounts) / len(amounts)", "sum(amounts) / len(amounts) if amounts else None"), encoding="utf-8")
        report["reference_repair_verification"] = command(working, "-m", "lab.verify")
        assert report["reference_repair_verification"]["exit_code"] == 0
        # Keep the original service buggy in the delivered package.
        assert "if amounts else None" not in (ROOT / "app" / "orders.py").read_text(encoding="utf-8")
        report["detector_tests"] = command(ROOT, "-m", "unittest", "discover", "-s", "qa", "-p", "test_*.py", "-v")
        assert report["detector_tests"]["exit_code"] == 0
        report["qa_status"] = "PASS"
    output = ROOT / "docs" / "SELF_CHECK.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"qa_status": "PASS", "agent_executed": False, "github_pr_created": False, "report": str(output)}))


if __name__ == "__main__":
    main()

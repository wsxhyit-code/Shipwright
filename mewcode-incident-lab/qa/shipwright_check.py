"""Check real Shipwright adapters and CreatePR gates without any model credentials."""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from lab.configure import config, rules
from lab.evidence import make_server as make_evidence_server
from lab.init import initialize
from qa.selfcheck import ROOT, scenario


def main():
    from mewcode.config import ToolsetConfig, OpsBackendConfig, CreatePRConfig
    from mewcode.toolset import assemble_toolset
    from mewcode.tools import create_default_registry
    from mewcode.tools.create_pr import CreatePRTool, CreatePRParams
    from mewcode.tools.ops.tools import ListAlertsParams, QueryLogsParams, QueryMetricsParams, GetLogSampleParams
    from mewcode.validator import validate_toolset
    from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, RuleEngine, PermissionMode

    report = {"scope": "actual Shipwright HTTP adapters/toolset/CreatePR; real local logs",
              "llm_executed": False, "github_pr_created": False}
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        runtime = folder / "runtime"
        initialize(runtime)
        scenario(runtime, None, "/api/orders/summary?customer_id=1002")
        server = make_evidence_server("127.0.0.1", 0, runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw = config(evidence_url=f"http://127.0.0.1:{server.server_port}")
            normalized = validate_toolset(raw["toolset"])
            ts = ToolsetConfig(ops=[OpsBackendConfig(**entry) for entry in normalized["ops"]],
                               create_pr=CreatePRConfig(**normalized["create_pr"]))
            registry = create_default_registry()
            assembly = assemble_toolset(registry, ts, work_dir=ROOT)
            report["registered_tools"] = assembly.tool_names
            assert "CreatePR" in assembly.tool_names and "QueryLogs" in assembly.tool_names
            alerts = assembly.ops.list_alerts("order-service")
            assert len(alerts) == 1
            assert assembly.ops.get_alert(alerts[0].alert_id) is not None
            clusters = assembly.ops.query_logs("order-service", "15m", "ERROR")
            assert sum(item.count for item in clusters) == 12
            assert any("ZeroDivisionError" in line for item in clusters for line in assembly.ops.get_log_sample(item.cluster_id, 3))
            time.sleep(1.1)
            metrics = assembly.ops.query_metrics("http_5xx_rate", "1m", "endpoint")
            assert metrics and metrics[0].latest == 100
            tool_results = {}
            for name, params in [("ListAlerts", ListAlertsParams(service="order-service")),
                                 ("QueryLogs", QueryLogsParams(service="order-service")),
                                 ("GetLogSample", GetLogSampleParams(cluster_id=clusters[0].cluster_id)),
                                 ("QueryMetrics", QueryMetricsParams(metric="http_5xx_rate", window="1m", group_by="endpoint"))]:
                result = asyncio.run(registry.get(name).execute(params))
                assert not result.is_error, result.output
                tool_results[name] = result.output
            report["real_tools"] = tool_results
            permissions = folder / "permissions.local.yaml"
            permissions.write_text(json.dumps(rules()), encoding="utf-8")
            checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(ROOT)),
                                        RuleEngine(local_rules_path=permissions), PermissionMode.DEFAULT)
            assert checker.check(registry.get("EditFile"), {"file_path": str(ROOT / "app/orders.py")}).effect == "allow"
            assert checker.check(registry.get("EditFile"), {"file_path": str(ROOT / "acceptance/test_regression.py")}).effect == "deny"
            assert checker.check(registry.get("Bash"), {"command": "python -m lab.verify"}).effect == "allow"
            assert checker.check(registry.get("CreatePR"), {"title": "Fix empty orders"}).effect == "allow"
            report["scoped_permissions_checked"] = True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            # Composite has no close() API; close individual HTTP providers.
            for name in ("alerts", "logs", "metrics"):
                backend = assembly.ops._providers.get(name)
                if backend and hasattr(backend, "close"):
                    backend.close()
        working = folder / "business-repo"
        shutil.copytree(ROOT, working, ignore=shutil.ignore_patterns("runtime", "runtime-*", "__pycache__", ".git", ".mewcode"))
        for command in [["git", "init", "-b", "main"], ["git", "add", "."],
                        ["git", "-c", "user.name=Lab QA", "-c", "user.email=lab@example.invalid", "commit", "-m", "baseline"]]:
            subprocess.run(command, cwd=working, check=True, capture_output=True)
        verifier_cmd = f'"{sys.executable}" -m lab.verify'
        tool = CreatePRTool(str(working), verify_command=verifier_cmd, require_independent=True)
        params = CreatePRParams(title="Fix empty order summary", description="Lab gate exercise")
        before = asyncio.run(tool.execute(params))
        assert before.is_error and "验证未通过" in before.output
        target = working / "app/orders.py"
        target.write_text(target.read_text(encoding="utf-8").replace("sum(amounts) / len(amounts)",
                          "sum(amounts) / len(amounts) if amounts else None"), encoding="utf-8")
        after = asyncio.run(tool.execute(params))
        assert after.is_error and "没有配置验证者" in after.output
        assert not (working / ".mewcode/pr/changes.patch").exists()
        report["create_pr_before_repair"] = before.output
        report["create_pr_without_independent_verifier"] = after.output
        report["config_schema_checked"] = True
        report["permission_rules_count"] = len(rules())
        report["qa_status"] = "PASS"
    path = ROOT / "docs/SHIPWRIGHT_CHECK.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"qa_status": "PASS", "llm_executed": False, "github_pr_created": False, "report": str(path)}))


if __name__ == "__main__":
    main()

"""真跑 HTTP 的运维后端 demo。

`tests/test_ops_backends.py` 用 `httpx.MockTransport` 拦请求 —— 快、确定性强，
但有个不可避免的疑点：「万一真发 HTTP 的时候行为不一样呢？」

这个 demo 在本地起一个**真的 HTTP 服务**（`127.0.0.1`，真 socket），
让 `LokiLogBackend` / `PrometheusMetricBackend` / `AlertmanagerBackend`
对着它跑。数据用的还是 `MockOpsBackend` 那个 orders-api 故障场景。

跑法：

    python docs/devops-agent/demo_real_backends.py

零 API key，零外部依赖。

⚠️ 改这个文件时注意：它含中文，**不要用 PowerShell 的
`Get-Content -Raw | Set-Content` 做文本替换** —— 那会按 ANSI 解码 UTF-8
再写回去，中文全变成乱码（写这个 demo 时踩过一次）。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mewcode.tools.ops.backends import (  # noqa: E402
    AlertmanagerBackend,
    CompositeOpsBackend,
    LokiLogBackend,
    OpsCapabilityMissing,
    PrometheusMetricBackend,
)

SERVICE = "orders-api"

# ---------------------------------------------------------------------------
# 造数据：和 MockOpsBackend 的场景一致，但数量级拉到真实水平
# ---------------------------------------------------------------------------

#: 2026-09-29 10:23:41 UTC 的纳秒时间戳。
#: 必须用 datetime 算出来 —— 手写常量会和日志文本里的时间对不上，
#: 于是显示出来的时间范围是错的（这个坑踩过一次）。
_BASE_NS = int(datetime(2026, 9, 29, 10, 23, 41, tzinfo=timezone.utc).timestamp() * 1e9)


def build_log_lines() -> list[tuple[str, str]]:
    """返回 (纳秒时间戳, 原始日志行) 列表。

    1,204 条 NPE + 37 条超时 + 60 条正常 INFO。
    数字和时间戳每行都不同 —— 这正是聚类要处理的情况。
    """
    out: list[tuple[str, str]] = []
    for i in range(1204):
        ns = _BASE_NS + i * 740_000_000
        sec = 41 + (i // 60) % 60
        out.append(
            (
                str(ns),
                f"2026-09-29 10:{23 + (i // 3600)}:{sec:02d}.{i % 1000:03d} ERROR "
                f"[orders-api,pod-3] c.a.o.OrderController - unhandled exception "
                f'java.lang.NullPointerException: Cannot invoke "com.acme.user.Profile'
                f'.getTier()" because the return value of "com.acme.order.OrderService'
                f'.getProfile()" is null at OrderService.getTier(OrderService.java:142) '
                f"trace={i:08x}",
            )
        )
    for i in range(37):
        ns = _BASE_NS + (i + 62) * 900_000_000
        out.append(
            (
                str(ns),
                f"2026-09-29 10:24:{(i * 2) % 60:02d}.{(i * 7) % 1000:03d} WARN "
                f"[orders-api,pod-3] c.a.o.PaymentClient - charge timeout after "
                f"{3000 + i}ms order={90000 + i}",
            )
        )
    for i in range(60):
        ns = _BASE_NS + i * 20_000_000_000
        out.append(
            (
                str(ns),
                f"2026-09-29 10:20:{i % 60:02d}.000 INFO [orders-api,pod-1] "
                f"c.a.o.OrderController - handled request {5000 + i} in {12 + i}ms",
            )
        )
    return out


def build_prom_series(query: str = "") -> dict:
    """12 个点的指标，只有 pod-3 的 http_5xx_rate 在中间飙起来。

    `query` 会被真的用来过滤 —— 假服务必须和真 Prometheus 一样尊重查询，
    否则 demo 会拿到自己没要的指标（这个坑踩过一次：控制组数据混进了主表）。
    """
    pods = {
        "pod-1": [0.1, 0.1, 0.1, 0.2, 0.1, 0.2, 0.1, 0.1, 0.2, 0.1, 0.1, 0.2],
        "pod-2": [0.1, 0.1, 0.1, 0.1, 0.1, 0.2, 0.1, 0.1, 0.1, 0.1, 0.2, 0.1],
        "pod-3": [0.1, 0.1, 0.2, 0.1, 8.4, 9.9, 11.2, 11.9, 12.1, 12.4, 12.2, 12.3],
    }
    end = int(time.time())
    step = 75
    result = []
    for pod, values in pods.items():
        for name, series in (
            ("http_5xx_rate", values),
            # 对照组：cpu 一直平稳，用来证明"异常检测没在乱报"
            ("cpu_usage", [round(0.31 + i * 0.001, 4) for i in range(12)]),
        ):
            if query and name not in query:
                continue
            result.append(
                {
                    "metric": {"__name__": name, "pod": pod},
                    "values": [
                        [end - (len(series) - 1 - i) * step, str(v)]
                        for i, v in enumerate(series)
                    ],
                }
            )
    return {"status": "success", "data": {"result_type": "matrix", "result": result}}


AM_ALERTS = [
    {
        "labels": {"alertname": "HTTP5xxHigh", "service": SERVICE,
                   "severity": "critical", "region": "cn-east-1"},
        "annotations": {"summary": "5xx 占比 5 分钟内从 0.1% 升到 12.3%"},
        "startsAt": "2026-09-29T10:38:00Z",
    },
    {
        "labels": {"alertname": "P99LatencyHigh", "service": SERVICE, "severity": "warning"},
        "annotations": {"summary": "P99 从 180ms 升到 2.4s"},
        "startsAt": "2026-09-29T10:36:00Z",
    },
    {
        "labels": {"alertname": "DiskFill", "service": "billing-worker", "severity": "warning"},
        "annotations": {"summary": "磁盘使用率 91%"},
        "startsAt": "2026-09-29T07:12:00Z",
    },
]


# ---------------------------------------------------------------------------
# 假的 Loki / Prometheus / Alertmanager
# ---------------------------------------------------------------------------

_LEVEL_RE = re.compile(r'level=~"\(\?i\)(\w+)"')
_PATTERN_RE = re.compile(r'\|~ "\(\?i\)([^"]+)"')


class Handler(BaseHTTPRequestHandler):
    """只实现这三个后端会打到的那三个 endpoint。"""

    protocol_version = "HTTP/1.1"
    log_lines: list[tuple[str, str]] = []
    hits: list[str] = []

    def log_message(self, *args):  # 别把访问日志刷到屏幕上
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        type(self).hits.append(parsed.path)

        if parsed.path == "/loki/api/v1/query_range":
            payload = self._loki(params)
        elif parsed.path == "/api/v1/query_range":
            payload = build_prom_series(params.get("query", ""))
        elif parsed.path == "/api/v2/alerts":
            payload = AM_ALERTS
        else:
            self.send_error(404, "not found")
            return

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _loki(self, params: dict[str, str]) -> dict:
        """真的按 LogQL 里的 level 和 |~ 过滤 —— 不然这个假服务就是在骗人。"""
        query = params.get("query", "")
        lines = list(self.log_lines)

        m = _LEVEL_RE.search(query)
        if m:
            want = m.group(1).upper()
            lines = [x for x in lines if want in x[1].upper()]

        m = _PATTERN_RE.search(query)
        if m:
            rx = re.compile(m.group(1), re.I)
            lines = [x for x in lines if rx.search(x[1])]

        limit = int(params.get("limit", "5000"))
        lines = lines[:limit]

        # 按 stream 分组（真 Loki 是这么返回的）
        by_stream: dict[str, list] = {}
        for ns, line in lines:
            level = (
                "ERROR" if " ERROR " in line
                else "WARN" if " WARN " in line
                else "INFO"
            )
            key = json.dumps({"level": level, "service": SERVICE}, sort_keys=True)
            by_stream.setdefault(key, []).append([ns, line])

        return {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {"stream": json.loads(k), "values": v} for k, v in by_stream.items()
                ],
            },
        }


def start_server() -> tuple[ThreadingHTTPServer, str]:
    Handler.log_lines = build_log_lines()
    Handler.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    srv, base = start_server()
    raw_count = len(Handler.log_lines)
    print(f"已在 {base} 起了一个真的 HTTP 服务（真 socket，不是 mock transport）")
    print(f"里面的日志数据：{raw_count} 行（1,204 NPE + 37 超时 + 60 INFO）")

    try:
        backend = CompositeOpsBackend(
            alerts=AlertmanagerBackend(base),
            logs=LokiLogBackend(base),
            metrics=PrometheusMetricBackend(base),
            # deploys / health / incidents 故意不接
        )

        hr("⓪ 排查开始前，先把能力清单给模型看")
        print(backend.describe())
        print()
        print("→ 关键：让模型提前知道「部署没接」，它就不会去依赖一个不存在的数据源。")

        hr("① ListAlerts —— 查告警")
        for a in backend.list_alerts(service=SERVICE):
            print(f"  [{a.severity:8}] {a.at}  {a.alert_id}: {a.detail}")
        print()
        print("  （billing-worker 的 DiskFill 被 service=orders-api 过滤掉了）")

        hr("② QueryLogs —— 查日志（这里是最关键的一步）")
        t0 = time.perf_counter()
        clusters = backend.query_logs(SERVICE, "30m", level="ERROR")
        elapsed = (time.perf_counter() - t0) * 1000
        total = sum(c.count for c in clusters)
        print(f"  真实 HTTP 往返 {elapsed:.1f}ms")
        print()
        print(f"  第一次查 level=ERROR：取回 {total} 行原始日志")
        for c in clusters:
            print(f"  {c.cluster_id}  ×{c.count:<5} {c.first_seen} ~ {c.last_seen}")
            print(f"      {c.signature[:110]}")
        print()
        print(f"  → {total} 行塌成了 {len(clusters)} 个聚类。")
        print("    签名把 trace=00a1b2c3 这类每次都变的东西换成了占位符，")
        print("    剩下的结构（哪个类、哪个方法、哪一行）才是有信息量的部分。")

        error_lines = [x for x in Handler.log_lines if " ERROR " in x[1]]
        approx_chars = sum(len(x[1]) for x in error_lines)
        print()
        print(f"  如果直接把 {len(error_lines)} 行 ERROR 塞进上下文会怎样：")
        print(f"    ≈ {approx_chars:,} 字符 ≈ {approx_chars // 3:,} tokens")
        print("    超过 SINGLE_RESULT_CHAR_LIMIT(50,000) → 落盘 → 模型只看到 2,000 字符预览")
        print("    ★ 模型看到的会是「一个文件路径」，而不是「NPE 在 OrderService.java:142」")

        hr("③ GetLogSample —— 确认聚类里到底是什么")
        for line in backend.get_log_sample(clusters[0].cluster_id, 1):
            print(f"  {line[:190]}")
        print()
        print("  → 只有这一行。要看细节时才拉，而且有上限。")
        print(f"    聚类 ID {clusters[0].cluster_id} 是从签名算出来的 ——")
        print("    同一个错误换个时间窗口再查，ID 还是它，可以直接对照。")

        hr("④ 再查一次 WARN —— 排查时不会只看 ERROR")
        warn = backend.query_logs(SERVICE, "30m", level="WARN")
        for c in warn:
            print(f"  {c.cluster_id}  ×{c.count:<5} {c.first_seen} ~ {c.last_seen}")
            print(f"      {c.signature[:110]}")
        print()
        print("  → 超时是 NPE 的伴生现象（profile 拿不到 → 下游调用超时），")
        print("    不是独立故障。分清主次才不会被第二类错误带偏。")
        print()
        print("  注意：上面 ERROR 聚类的 ID 现在去 GetLogSample 会报「不在最近一次结果里」。")
        print("  这是刻意的 —— ID 由签名派生，绝不做跨查询的错配。")
        print("  如果用位置序号 C-1 / C-2，ERROR 的 C-1 会静默变成 WARN 的 C-1，")
        print("  agent 拿到另一个聚类的原始行，而且完全不会察觉。")

        hr("⑤ QueryMetrics —— 查指标（分组对比）")
        series = backend.query_metrics("http_5xx_rate", "30m", "pod")
        print(f"  {'pod':<8}{'基线':>8}{'最新':>8}   异常？")
        for s in sorted(series, key=lambda x: x.group):
            flag = "⚠️  是" if s.is_anomalous() else "否"
            print(f"  {s.group:<8}{s.baseline:>8.1f}{s.latest:>8.1f}   {flag}")
        print()
        print("  → 只有 pod-3 异常。单 pod 异常 + 刚刚有发布 = 指向那次发布，")
        print("    而不是「服务整体有问题」—— 这两种判断的处理方式完全不同。")

        print()
        control = backend.query_metrics("cpu_usage", "30m", "pod")
        print("  对照组 cpu_usage（平稳）：")
        for s in sorted(control, key=lambda x: x.group):
            print(
                f"  {s.group:<8}{s.baseline:>8.3f}{s.latest:>8.3f}   "
                f"{'⚠️  是' if s.is_anomalous() else '否'}"
            )
        print("  → CPU 正常，说明不是「机器扛不住」，是代码问题。（异常检测没在乱报）")

        hr("⑥ ListDeploys —— 部署没接，必须报错")
        try:
            backend.list_deploys(SERVICE)
            print("  ❌ 不该走到这里")
            return 1
        except OpsCapabilityMissing as e:
            print(f"  OpsCapabilityMissing: {e}")
        print()
        print("  ★ 这是整个设计里最容易被忽略、但后果最严重的一点：")
        print("    如果这里返回 []，模型会读成「10:23 前后没有部署」，")
        print("    于是排除掉唯一正确的根因，并且在报告里写得非常自信。")
        print("    静默的空结果在故障排查里比报错危险得多。")

        hr("⑦ 走到这里，模型会去干什么")
        print("  日志堆栈里有 `OrderService.java:142` —— 这是个代码位置。")
        print("  普通运维 AI 只能说「建议重启 pod-3」；")
        print("  带 coding 能力的 agent 会直接去读那个文件，")
        print("  然后产出补丁走 CreatePR 门禁（见 demo_verify_gate.py）。")
        print()
        print("  两半合起来才是「云端代码运维 agent」：")
        print("    运维工具负责「发现是哪一行代码」，")
        print("    coding 能力负责「改对那一行并且自己先验证过」。")

        hr("HTTP 请求记录")
        for path in Handler.hits:
            print(f"  GET {path}")
        print(f"  共 {len(Handler.hits)} 次真实 HTTP 请求")

        print()
        print("=" * 78)
        print("全部通过：三个真实后端在真 socket 上跑通了完整排查链路")
        print("=" * 78)
        return 0
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

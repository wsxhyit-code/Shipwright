"""运维后端抽象：日志 / 指标 / 部署 / 告警 / 工单。

工具层只依赖这些方法。接真实系统时实现 `OpsBackend`（Loki / Prometheus /
CI 平台 / PagerDuty 都能对上一个方法），**工具代码一行都不用改**。

`MockOpsBackend` 是内存实现，内含一个"部署完 12 分钟后开始大规模报错"的
真实故障场景，用于跑通整条故障定位链路。

## 一个关键设计：QueryLogs 返回的是**聚类**，不是原始日志

如果 `QueryLogs` 直接把匹配到的 1,204 条日志返回，那就是把 240KB 塞进上下文
——正好踩上"工具结果超限→落盘→只给 2KB 预览"那个坑。

所以这里的接口是两层，对应"Grep 定位 → 局部读"的思路：

    QueryLogs     → 按错误签名聚类，只给「类型 / 条数 / 时间范围 / 样例一行」
    GetLogSample  → 针对某一个聚类，取少量原始行（有上限）

模型看聚类就能定位到"是哪个错误"，需要细节时再拉少量原始行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# 领域模型
# ---------------------------------------------------------------------------


@dataclass
class LogCluster:
    """一类错误的聚合视图。"""

    cluster_id: str
    signature: str          # 错误签名（异常类型 + 位置）
    count: int
    first_seen: str         # HH:MM:SS
    last_seen: str
    sample: list[str] = field(default_factory=list)   # 少量原始行
    services: list[str] = field(default_factory=list)


@dataclass
class MetricSeries:
    name: str
    group: str              # 分组键（pod / region / endpoint…）
    values: list[tuple[str, float]] = field(default_factory=list)  # (HH:MM, value)

    @property
    def latest(self) -> float:
        return self.values[-1][1] if self.values else 0.0

    @property
    def baseline(self) -> float:
        """窗口**起始值**，作为"异常之前是多少"的参考。

        ⚠️ 不能用"前几个点的中位数"：如果尖峰从第二个点就开始了，
        中位数会把尖峰算进基线，异常就检测不出来（这个坑实测踩过）。
        窗口的第一个点才是"还没出问题"的状态。
        """
        return self.values[0][1] if self.values else 0.0

    def is_anomalous(self) -> bool:
        """相对翻倍 **且** 绝对增量有意义，才算异常。

        只看相对倍数会把噪声当成故障：5xx 从 0.1% 涨到 0.2% 是 2 倍，
        但完全没有意义。所以加一个 `base + 1.0` 的绝对地板 ——
        它对「百分比」和「毫秒」这两类量纲都合适。
        """
        base, now = self.baseline, self.latest
        if base <= 0:
            return False
        return now > max(base * 2, base + 1.0)


@dataclass
class Deploy:
    deploy_id: str
    service: str
    version: str
    at: str                 # HH:MM
    targets: list[str] = field(default_factory=list)   # pod 列表
    commit: str = ""
    author: str = ""


@dataclass
class Alert:
    alert_id: str
    service: str
    name: str
    severity: str           # critical / warning / info
    at: str
    detail: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Incident:
    incident_id: str
    service: str
    title: str
    severity: str
    root_cause: str = ""
    evidence: str = ""
    status: str = "open"


# ---------------------------------------------------------------------------
# 后端接口
# ---------------------------------------------------------------------------


class OpsBackend(Protocol):
    """工具层只依赖这些方法。全部是**只读**的 —— 运维动作里唯一该给 AI 的。"""

    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]: ...
    def get_alert(self, alert_id: str) -> Alert | None: ...
    def query_logs(
        self, service: str, window: str, level: str = "ERROR", pattern: str = ""
    ) -> list[LogCluster]: ...
    def get_log_sample(self, cluster_id: str, count: int) -> list[str]: ...
    def query_metrics(
        self, metric: str, window: str, group_by: str
    ) -> list[MetricSeries]: ...
    def list_deploys(self, service: str = "", limit: int = 10) -> list[Deploy]: ...
    def get_service_health(self, service: str) -> dict[str, Any]: ...
    def create_incident(
        self, service: str, title: str, severity: str, root_cause: str, evidence: str
    ) -> Incident: ...
    def describe(self) -> str: ...


class OpsError(Exception):
    """业务校验失败。工具层会转成 is_error=True 的可读结果。"""


# ---------------------------------------------------------------------------
# 内存实现：一个真实的故障场景
# ---------------------------------------------------------------------------


class MockOpsBackend:
    """场景：`orders-api` 在 10:11 滚动更新到 pod-3 后，10:23 开始大规模 NPE。

    线索是分散的，需要 agent 自己串：
      · 告警说 5xx 涨了
      · 日志聚类指向 OrderService.java:142 的 NPE
      · 指标显示**只有 pod-3 异常**
      · 部署记录显示 pod-3 在 12 分钟前更新过
      · 于是要去看这次发布的代码改动（这一步用 coding 工具做）
    """

    #: 演示场景里八个能力全都有，所以没有缺口
    capability: str = "mock"

    def __init__(self) -> None:
        self._alerts: list[Alert] = [
            Alert(
                alert_id="AL-7781",
                service="orders-api",
                name="HTTP 5xx rate high",
                severity="critical",
                at="10:38",
                detail="5xx 占比 5 分钟内从 0.1% 升到 12.3%",
                labels={"region": "cn-east-1", "env": "prod"},
            ),
            Alert(
                alert_id="AL-7779",
                service="orders-api",
                name="P99 latency high",
                severity="warning",
                at="10:36",
                detail="P99 从 180ms 升到 2.4s",
                labels={"region": "cn-east-1", "env": "prod"},
            ),
        ]

        self._logs: dict[str, LogCluster] = {
            "C-1": LogCluster(
                cluster_id="C-1",
                signature="java.lang.NullPointerException at OrderService.getTier(OrderService.java:142)",
                count=1204,
                first_seen="10:23:41",
                last_seen="10:38:02",
                services=["orders-api"],
                sample=[
                    "2026-09-29 10:23:41.203 ERROR [orders-api,pod-3] c.a.o.OrderController - "
                    "unhandled exception",
                    "java.lang.NullPointerException: Cannot invoke "
                    '"com.acme.user.Profile.getTier()" because the return value of '
                    '"com.acme.order.OrderService.getProfile()" is null',
                    "\tat com.acme.order.OrderService.getTier(OrderService.java:142)",
                    "\tat com.acme.order.OrderController.list(OrderController.java:88)",
                ],
            ),
            "C-2": LogCluster(
                cluster_id="C-2",
                signature="java.util.concurrent.TimeoutException at PaymentClient.charge(PaymentClient.java:61)",
                count=37,
                first_seen="10:24:02",
                last_seen="10:37:55",
                services=["orders-api"],
                sample=[
                    "2026-09-29 10:24:02.881 WARN [orders-api,pod-3] c.a.o.PaymentClient - "
                    "charge timeout after 3000ms",
                ],
            ),
        }

        self._metrics: dict[str, dict[str, MetricSeries]] = {
            "http_5xx_rate": {
                "pod-1": MetricSeries("http_5xx_rate", "pod-1", [
                    ("10:20", 0.1), ("10:25", 0.1), ("10:30", 0.2), ("10:38", 0.2)]),
                "pod-2": MetricSeries("http_5xx_rate", "pod-2", [
                    ("10:20", 0.1), ("10:25", 0.1), ("10:30", 0.1), ("10:38", 0.1)]),
                "pod-3": MetricSeries("http_5xx_rate", "pod-3", [
                    ("10:20", 0.1), ("10:25", 8.4), ("10:30", 11.9), ("10:38", 12.3)]),
            },
            "http_p99_ms": {
                "pod-1": MetricSeries("http_p99_ms", "pod-1", [
                    ("10:20", 175), ("10:25", 180), ("10:30", 178), ("10:38", 182)]),
                "pod-3": MetricSeries("http_p99_ms", "pod-3", [
                    ("10:20", 180), ("10:25", 1802), ("10:30", 2310), ("10:38", 2405)]),
            },
            "cpu_usage": {
                "pod-3": MetricSeries("cpu_usage", "pod-3", [
                    ("10:20", 0.31), ("10:25", 0.34), ("10:30", 0.33), ("10:38", 0.32)]),
            },
        }

        self._deploys: list[Deploy] = [
            Deploy("D-5521", "orders-api", "v2.14.3", "10:11", ["pod-3"],
                   commit="9b2c1f4", author="zhangsan"),
            Deploy("D-5518", "orders-api", "v2.14.2", "09:30", ["pod-1", "pod-2", "pod-3"],
                   commit="4a7e0d1", author="lisi"),
            Deploy("D-5510", "orders-api", "v2.14.1", "08:05", ["pod-1", "pod-2", "pod-3"],
                   commit="c81f2aa", author="zhangsan"),
        ]

        self._health: dict[str, dict[str, Any]] = {
            "orders-api": {
                "service": "orders-api",
                "status": "degraded",
                "pods": {"pod-1": "healthy", "pod-2": "healthy", "pod-3": "unhealthy"},
                "restarts_5m": {"pod-1": 0, "pod-2": 0, "pod-3": 0},
                "note": "pod-3 自 10:23 起持续返回 5xx",
            }
        }
        self._incidents: list[Incident] = []

    # --- 告警 ---

    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]:
        out = list(self._alerts)
        if service:
            out = [a for a in out if a.service == service]
        if severity:
            out = [a for a in out if a.severity == severity]
        order = {"critical": 0, "warning": 1, "info": 2}
        out.sort(key=lambda a: (order.get(a.severity, 9), a.at))
        return out

    def get_alert(self, alert_id: str) -> Alert | None:
        return next((a for a in self._alerts if a.alert_id == alert_id), None)

    # --- 日志（返回聚类，不返回原始日志）---

    def query_logs(
        self, service: str, window: str, level: str = "ERROR", pattern: str = ""
    ) -> list[LogCluster]:
        out = [c for c in self._logs.values() if not service or service in c.services]
        if pattern:
            p = pattern.lower()
            out = [c for c in out if p in c.signature.lower()]
        out.sort(key=lambda c: c.count, reverse=True)
        return out

    def get_log_sample(self, cluster_id: str, count: int) -> list[str]:
        c = self._logs.get(cluster_id)
        if c is None:
            raise OpsError(f"日志聚类 {cluster_id} 不存在")
        return c.sample[: max(1, count)]

    # --- 指标 ---

    def query_metrics(
        self, metric: str, window: str, group_by: str
    ) -> list[MetricSeries]:
        series = self._metrics.get(metric)
        if series is None:
            raise OpsError(
                f"指标 {metric} 不存在。可用：{', '.join(sorted(self._metrics))}"
            )
        return list(series.values())

    # --- 部署 ---

    def list_deploys(self, service: str = "", limit: int = 10) -> list[Deploy]:
        out = [d for d in self._deploys if not service or d.service == service]
        return out[: max(1, limit)]

    # --- 健康 ---

    def get_service_health(self, service: str) -> dict[str, Any]:
        h = self._health.get(service)
        if h is None:
            raise OpsError(f"服务 {service} 不存在")
        return h

    # --- 工单（唯一的写操作）---

    def create_incident(
        self, service: str, title: str, severity: str, root_cause: str, evidence: str
    ) -> Incident:
        if not title.strip():
            raise OpsError("故障标题不能为空")
        if severity not in ("critical", "major", "minor"):
            raise OpsError(f"严重度必须是 critical / major / minor，收到 {severity}")
        # 证据链是这一层最看重的东西：没有证据的根因等于猜测
        if not evidence.strip():
            raise OpsError("必须提供证据链（日志/指标/部署的具体线索），不接受无依据的根因")
        inc = Incident(
            incident_id=f"INC-{len(self._incidents) + 1:04d}",
            service=service,
            title=title,
            severity=severity,
            root_cause=root_cause,
            evidence=evidence,
        )
        self._incidents.append(inc)
        return inc

    # --- 自描述 ---

    def describe(self) -> str:
        """给模型看的可用性说明。

        和 `CompositeOpsBackend.describe()` 保持**同一个接口**。

        之前这里没有这个方法，导致 mock 场景下 `ops_capability_report()`
        返回空字符串 —— 于是"所有能力都接了"和"根本没生成报告"在调用方
        看起来一模一样。凡是"查不到"和"没有"会被混淆的地方，都要让二者可区分。
        """
        return (
            "运维后端能力清单（演示场景 MockOpsBackend）：\n"
            "  ✓ 告警：内置 AL-7781 / AL-7779\n"
            "  ✓ 日志：内置聚类 C-1 / C-2\n"
            "  ✓ 指标：http_5xx_rate / http_p99_ms / cpu_usage\n"
            "  ✓ 部署：内置 D-5521 / D-5518 / D-5510\n"
            "  ✓ 服务健康、工单\n"
            "注意：这是内存里的演示数据，不连任何真实系统。"
        )

    def close(self) -> None:
        """无外部资源，但保留这个方法以便和真实后端互换。"""

"""把多个专用后端拼成一个 `OpsBackend`。

真实环境里没有哪个系统同时提供日志、指标、告警、部署、工单，所以：

    backend = CompositeOpsBackend(
        alerts=AlertmanagerBackend("https://am.internal"),
        logs=LokiLogBackend("https://loki.internal", token=...),
        metrics=PrometheusMetricBackend("https://prom.internal"),
        deploys=MyArgoCdAdapter(...),        # 自己的适配器
    )

没接的能力**报错而不是返回空** —— 这一点是刻意的，见 `OpsCapabilityMissing`
的注释：静默的空结果会让 agent 把"没接入"读成"没有异常"，然后基于缺失的
信息给出一个看起来很有把握的错误结论。

`describe()` 能在排查开始前就把"哪些数据源可用"告诉模型，
这样它不会去依赖一个根本没接的东西。
"""
from __future__ import annotations

from typing import Any

from mewcode.tools.ops.backend import (
    Alert,
    Deploy,
    Incident,
    LogCluster,
    MetricSeries,
    OpsError,
)
from mewcode.tools.ops.backends.http_backends import OpsCapabilityMissing

#: 协议方法 -> (依赖的能力名, 能力的中文说明)
_ROUTING: dict[str, tuple[str, str]] = {
    "list_alerts": ("alerts", "告警"),
    "get_alert": ("alerts", "告警"),
    "query_logs": ("logs", "日志"),
    "get_log_sample": ("logs", "日志"),
    "query_metrics": ("metrics", "指标"),
    "list_deploys": ("deploys", "部署"),
    "get_service_health": ("health", "服务健康"),
    "create_incident": ("incidents", "工单"),
}

#: 能力名 -> 未接入时的提示（告诉使用者该接什么）
_HINTS = {
    "alerts": "接 AlertmanagerBackend，或你自己的告警系统适配器",
    "logs": "接 LokiLogBackend，或你自己的日志系统适配器",
    "metrics": "接 PrometheusMetricBackend，或你自己的指标系统适配器",
    "deploys": "接 ArgoCD / Jenkins / GitLab CI 的适配器",
    "health": "接你的服务健康检查 / K8s 适配器",
    "incidents": "接 PagerDuty / Jira / 内部工单系统的适配器",
}


class CompositeOpsBackend:
    """按方法把调用路由到对应的专用后端。

    每个参数都是可选的；只填你有的。缺的能力在**被调用时**抛
    `OpsCapabilityMissing`，错误信息里会带上"该接什么"。
    """

    def __init__(
        self,
        alerts: Any = None,
        logs: Any = None,
        metrics: Any = None,
        deploys: Any = None,
        health: Any = None,
        incidents: Any = None,
    ) -> None:
        self._providers: dict[str, Any] = {
            "alerts": alerts,
            "logs": logs,
            "metrics": metrics,
            "deploys": deploys,
            "health": health,
            "incidents": incidents,
        }
        # 构造期就做一次校验：参数写错（比如把 Loki 传给了 metrics）
        # 应该在启动时炸，而不是等到半夜排查故障时才发现
        self._verify_signatures()

    def _verify_signatures(self) -> None:
        for capability, provider in self._providers.items():
            if provider is None:
                continue
            for method, (needed, label) in _ROUTING.items():
                if needed != capability:
                    continue
                if not hasattr(provider, method):
                    raise OpsError(
                        f"{capability} 后端 {type(provider).__name__} "
                        f"缺少 {label} 能力所需的 `{method}` 方法"
                    )

    # --- 路由 ---

    def _route(self, method: str) -> Any:
        capability, label = _ROUTING[method]
        provider = self._providers.get(capability)
        if provider is None:
            raise OpsCapabilityMissing(
                f"{label} 能力未接入，`{method}` 无法执行。"
                f"（{_HINTS.get(capability, '请提供对应适配器')}）"
                f" 注意：这不代表「没有{label}」，只是没有接入数据源。"
            )
        return getattr(provider, method)

    # --- OpsBackend 协议 ---

    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]:
        return self._route("list_alerts")(service=service, severity=severity)

    def get_alert(self, alert_id: str) -> Alert | None:
        return self._route("get_alert")(alert_id)

    def query_logs(
        self, service: str, window: str, level: str = "ERROR", pattern: str = ""
    ) -> list[LogCluster]:
        return self._route("query_logs")(
            service=service, window=window, level=level, pattern=pattern
        )

    def get_log_sample(self, cluster_id: str, count: int) -> list[str]:
        return self._route("get_log_sample")(cluster_id, count)

    def query_metrics(self, metric: str, window: str, group_by: str) -> list[MetricSeries]:
        return self._route("query_metrics")(metric, window, group_by)

    def list_deploys(self, service: str = "", limit: int = 10) -> list[Deploy]:
        return self._route("list_deploys")(service=service, limit=limit)

    def get_service_health(self, service: str) -> dict[str, Any]:
        return self._route("get_service_health")(service)

    def create_incident(
        self, service: str, title: str, severity: str, root_cause: str, evidence: str
    ) -> Incident:
        return self._route("create_incident")(
            service=service,
            title=title,
            severity=severity,
            root_cause=root_cause,
            evidence=evidence,
        )

    # --- 自描述 ---

    def wired(self) -> list[str]:
        """已接入的能力名（有序）。"""
        return [c for c, p in self._providers.items() if p is not None]

    def describe(self) -> str:
        """给模型看的可用性说明。

        提前把"哪些数据源可用"讲清楚，模型就不会去依赖一个没接的东西 ——
        比等它调用失败再纠正要省一轮，也避免它把缺失当成"没有异常"。
        """
        lines = ["运维后端能力清单："]
        for capability, label in dict.fromkeys(_ROUTING.values()):
            provider = self._providers.get(capability)
            if provider is None:
                lines.append(f"  ✗ {label}：未接入，调用会报错（≠ 没有{label}）")
            else:
                lines.append(f"  ✓ {label}：{type(provider).__name__}")
        return "\n".join(lines)

    def close(self) -> None:
        for p in self._providers.values():
            closer = getattr(p, "close", None)
            if callable(closer):
                closer()

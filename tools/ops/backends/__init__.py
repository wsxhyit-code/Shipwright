"""真实运维后端（可接实际系统）。

    LokiLogBackend         日志    /loki/api/v1/query_range
    PrometheusMetricBackend 指标   /api/v1/query_range
    AlertmanagerBackend    告警    /api/v2/alerts
    CompositeOpsBackend    把上面几个（加上你自己的适配器）拼成一个 OpsBackend

部署/工单没有通用标准，需要自己写适配器 —— 只要实现了对应方法就能交给
`CompositeOpsBackend`，`tools/ops/tools.py` 里的工具代码一行都不用改。
"""
from mewcode.tools.ops.backends.composite import CompositeOpsBackend
from mewcode.tools.ops.backends.http_backends import (
    AlertmanagerBackend,
    LokiLogBackend,
    OpsCapabilityMissing,
    PrometheusMetricBackend,
    log_signature,
    parse_window,
    window_bounds,
)

__all__ = [
    "AlertmanagerBackend",
    "CompositeOpsBackend",
    "LokiLogBackend",
    "OpsCapabilityMissing",
    "PrometheusMetricBackend",
    "log_signature",
    "parse_window",
    "window_bounds",
]

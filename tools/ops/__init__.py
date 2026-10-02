"""运维工具包：让 agent「能写代码，也能做运维」。

运维有六类工作，**只有两类该给 AI**：

    环境运维   起服务、配环境变量         → 平台做（见 docker/）
    构建运维   装依赖、编译、打镜像        → CI 做
    测试运维   跑测试、跑 lint            → CI 做
    发布运维   打 tag、部署、灰度、回滚     → CI 做（要确定性）
    ────────────────────────────────────────────────
    运行运维  看日志、查指标、部署状态      → ✅ agent 做
    故障运维  定位根因、给出证据链          → ✅✅ agent 最高价值

**刻意没有提供"回滚部署""重启服务"这类工具** —— 那些属于发布运维，
必须走有灰度、有审批、有回滚策略的确定性流水线。

## 和 coding agent 的结合点

故障定位到"是 142 行少了判空"这一步，需要**读代码**：

    QueryLogs(聚类) → 拿到 OrderService.java:142
        ↓
    Grep / ReadFile（coding 工具） → 看这行代码、追调用链
        ↓
    ListDeploys → 拿到 commit → 看这次发布改了什么

**这一步是普通运维 AI 做不到的** —— 它只能告诉你"pod-3 有问题，建议重启"，
而带 coding 能力的 agent 能告诉你"是 142 行少了判空，而且我知道是哪次改动引入的"。

用法：

    from mewcode.tools import create_default_registry
    from mewcode.tools.ops import MockOpsBackend, register_ops_tools

    registry = create_default_registry()
    register_ops_tools(registry, MockOpsBackend())
"""
from __future__ import annotations

from mewcode.tools.ops.backend import (
    Alert,
    Deploy,
    Incident,
    LogCluster,
    MetricSeries,
    MockOpsBackend,
    OpsBackend,
    OpsError,
)
from mewcode.tools.ops.tools import OPS_TOOLS, register_ops_tools

__all__ = [
    "Alert",
    "Deploy",
    "Incident",
    "LogCluster",
    "MetricSeries",
    "MockOpsBackend",
    "OPS_TOOLS",
    "OpsBackend",
    "OpsError",
    "register_ops_tools",
]

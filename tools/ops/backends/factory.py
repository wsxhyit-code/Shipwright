"""按配置构造运维后端。

把"配置里写了什么"翻译成"实际用哪个后端类"，这样用户不用写 Python 就能接：

    toolset:
      ops:
        - kind: alertmanager
          base_url: https://am.internal
          token_env: AM_TOKEN
          capability: alerts
        - kind: loki
          base_url: https://loki.internal
          token_env: LOKI_READONLY_TOKEN
          capability: logs

## 两条刻意的设计

**① token 只从环境变量读（`token_env`），不鼓励写进配置文件。**
配置文件通常进版本库；运维 token 进了版本库等于公开。`token` 字段仍然支持，
但它是给本地调试用的。

**② 环境变量缺失就在构造时炸，不静默降级成"无认证请求"。**
缺失时若照常发请求，得到的是一个 401 —— 而 401 在排查过程中很容易被
当成"服务端权限配置问题"，而不是"你的配置里 token 没读到"。
早点用一句能看懂的话报出来，比让人去猜 HTTP 状态码强。
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from mewcode.tools.ops.backend import MockOpsBackend, OpsError
from mewcode.tools.ops.backends.composite import CompositeOpsBackend
from mewcode.tools.ops.backends.http_backends import (
    AlertmanagerBackend,
    LokiLogBackend,
    PrometheusMetricBackend,
)

#: kind -> 后端类。mock 特判（它不接受 base_url）
_BACKENDS = {
    "loki": LokiLogBackend,
    "prometheus": PrometheusMetricBackend,
    "alertmanager": AlertmanagerBackend,
}


def _resolve_token(spec: dict, env: Mapping[str, str], label: str) -> str:
    token_env = (spec.get("token_env") or "").strip()
    if not token_env:
        return (spec.get("token") or "").strip()

    value = (env.get(token_env) or "").strip()
    if not value:
        raise OpsError(
            f"{label}: 环境变量 {token_env} 没有设置或为空。\n"
            f"配置里声明了 token_env: {token_env}，说明这个后端需要认证。\n"
            f"请在启动 agent 前导出它，例如：export {token_env}=<你的只读 token>\n"
            f"（如果这个后端确实允许匿名访问，请把 token_env 那一行删掉，"
            f"而不是留一个读不到的环境变量名。）"
        )
    return value


def build_ops_backend(
    specs: list[dict] | None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """把配置里的 ops 列表拼成一个 `OpsBackend`。

    `specs` 是**已经过 validator 校验**的 dict 列表（键：kind / capability /
    base_url / token_env / token / timeout）。返回 `CompositeOpsBackend`；
    没配任何后端时返回 `None`（调用方据此不注册运维工具）。

    写进来的能力是"按 capability 路由"的，所以部署 / 工单这类没有现成实现的
    能力，在配置里就是 **不写** —— 运行时调用会抛 `OpsCapabilityMissing`，
    这正是我们要的语义（"没接入" ≠ "没有异常"）。
    """
    specs = specs or []
    if not specs:
        return None

    env = os.environ if env is None else env
    providers: dict[str, Any] = {}

    for i, spec in enumerate(specs):
        kind = spec.get("kind")
        capability = spec.get("capability")
        label = f"toolset.ops[{i}] (kind={kind}, capability={capability})"

        if kind == "mock":
            # mock 是个完整场景对象，不能按能力拆 —— 只能独占
            if len(specs) > 1:
                raise OpsError(
                    f"{label}: kind=mock 不能和其它后端混用（它自己就是一个完整场景）"
                )
            return MockOpsBackend()

        cls = _BACKENDS.get(kind)
        if cls is None:
            raise OpsError(
                f"{label}: 不认识的后端类型 {kind!r}，"
                f"可选：{', '.join(sorted([*_BACKENDS, 'mock']))}"
            )

        providers[capability] = cls(
            base_url=spec.get("base_url", ""),
            token=_resolve_token(spec, env, label),
            timeout=float(spec.get("timeout", 15.0)),
        )

    return CompositeOpsBackend(**providers)


def ops_capability_report(backend: Any) -> str:
    """给模型看的可用性说明（`None` 时说明运维工具整体没启用）。"""
    if backend is None:
        return ""
    describe = getattr(backend, "describe", None)
    return describe() if callable(describe) else ""

"""插件机制：第三方**装个包**就能把自己的工具接进 agent，不用改核心代码。

## 为什么需要它

方向二要「接入私有工具链」。此前只有两条路：

  · 原生工具 —— 得改仓库代码（`tools/` 下加类 + 在装配处注册）
  · MCP server —— 需要额外起一个进程/服务

两条都不适合「公司内部已经有几十个自研系统的 SDK，只想包一层工具」这种场景。
插件机制给第三条路：

    # 第三方包的 pyproject.toml
    [project.entry-points."mewcode.tools"]
    acme = "acme_mewcode:register"

    # acme_mewcode/__init__.py
    def register(registry):
        registry.register(QueryAcmeOrders())
        return ["QueryAcmeOrders"]        # 返回值可选，用于日志

装完之后 `pip install acme-mewcode`，agent 立刻多出这些工具。

## entry point 的契约

函数签名 `register(registry) -> list[str] | None`：
  · `registry` 是 `ToolRegistry`，直接 `registry.register(tool)` 即可
  · 返回值是注册的工具名列表（可选，只用于展示和日志）

## 一个失败插件不该拖垮整个 agent

插件是**第三方代码**。它 import 失败、或者 `register()` 抛异常，
都不应该让 agent 起不来 —— 那会让一个坏插件把整个工具链一起带走。

但也**绝不能静默**：失败的插件会被记进 `PluginLoadResult.errors`，
并且由 `toolset` 把它写进给模型看的能力清单里。
「悄悄少了一批工具」是最坏的结果 —— agent 不知道自己没有那些能力，
会去猜、或者用别的方式硬凑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: entry point 组名。第三方在 pyproject.toml 里写这个组。
ENTRY_POINT_GROUP = "mewcode.tools"


@dataclass
class PluginLoadResult:
    """插件加载结果。"""

    loaded: list[str] = field(default_factory=list)      # 成功加载的插件名
    tools: list[str] = field(default_factory=list)       # 它们注册的工具名
    errors: list[tuple[str, str]] = field(default_factory=list)   # (插件名, 原因)

    @property
    def ok(self) -> bool:
        return not self.errors

    def describe(self) -> str:
        if not self.loaded and not self.errors:
            return ""
        lines = []
        if self.loaded:
            lines.append(
                f"插件：已加载 {len(self.loaded)} 个"
                f"（{', '.join(self.loaded)}）→ 新增 {len(self.tools)} 个工具"
            )
        if self.errors:
            lines.append("⚠️ **以下插件加载失败，它们的工具当前不存在**：")
            for name, why in self.errors:
                lines.append(f"    ✗ {name}：{why}")
            lines.append(
                "  注意：这不代表「那些系统里没有数据」，只是这些工具没装上。\n"
                "  不要用别的方式猜测这些工具本该返回什么。"
            )
        return "\n".join(lines)


def discover_entry_points() -> list[Any]:
    """发现所有声明了 `mewcode.tools` 的 entry point。

    单独抽成函数是为了**可测**：测试可以传假的 entry point 列表，
    不必真的去装一个第三方包。
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.10
        return []
    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except TypeError:  # pragma: no cover - 老式 API
        return list(entry_points().get(ENTRY_POINT_GROUP, []))


def load_plugin_tools(
    registry: Any,
    entry_points_list: Iterable[Any] | None = None,
    enabled: bool = True,
) -> PluginLoadResult:
    """把插件注册进 `registry`。

    `entry_points_list` 显式传入时用它（测试用）；否则自动发现。
    `enabled=False` 时直接返回空结果（配置里可以关掉插件）。
    """
    result = PluginLoadResult()
    if not enabled:
        return result

    eps = list(entry_points_list) if entry_points_list is not None else discover_entry_points()

    for ep in eps:
        name = getattr(ep, "name", "?")
        try:
            register = ep.load()
        except Exception as e:  # noqa: BLE001 - 第三方代码，什么都可能抛
            result.errors.append((name, f"加载入口失败：{type(e).__name__}: {e}"))
            continue

        if not callable(register):
            result.errors.append(
                (name, f"entry point 指向的不是可调用对象（是 {type(register).__name__}）")
            )
            continue

        before = _tool_names(registry)
        try:
            returned = register(registry)
        except Exception as e:  # noqa: BLE001
            result.errors.append((name, f"register() 抛异常：{type(e).__name__}: {e}"))
            continue

        after = _tool_names(registry)
        added = sorted(after - before)
        if not added:
            # 注册成功但一个工具都没加 —— 大概率是插件写错了，
            # 说清楚比记一条"加载成功"有用。
            result.errors.append(
                (name, "register() 没有注册任何工具（返回了 %r）" % (returned,))
            )
            continue

        result.loaded.append(str(name))
        result.tools.extend(added)

    return result


def _tool_names(registry: Any) -> set[str]:
    try:
        return {t.name for t in registry.list_tools()}
    except Exception:  # noqa: BLE001 - 不该因为读不出名字就崩
        return set()


__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginLoadResult",
    "discover_entry_points",
    "load_plugin_tools",
]

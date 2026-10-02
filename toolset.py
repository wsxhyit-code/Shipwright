"""把「配置里声明的能力」装进运行中的 agent。

## 为什么需要这个模块

在它存在之前，`tools/ops/` 和 `tools/create_pr.py` 是**能通过测试但够不着**的
代码：`app.py` 只调 `create_default_registry()`（6 个内置工具），然后把
worktree / agent / team / skill 这些工具挂上去 —— 运维工具和 CreatePR
一个都没挂。于是：

  · 测试证明"验证门禁本身是通的"
  · 但 `python -m mewcode` 起来的 agent 里**根本没有 CreatePR 这个工具**
  · 文档里"agent 自己验证通过才提 PR"的流程，用现在的入口跑不出来

这就是"接了但没通电"。这个模块就是那根线。

## 装配顺序

    create_default_registry()      6 个内置工具
      ↓
    app.py 挂 worktree / agent / team / skill …
      ↓
    assemble_toolset()             ← 运维工具 + CreatePR（本模块）
      ↓
    最终 registry

顺序不影响正确性（验证者的只读注册表是在**调用时**快照的），
但把这一段单独放一个函数，是为了让"这个 agent 到底有哪些能力"
在一个地方就能读全。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mewcode.config import ToolsetConfig
from mewcode.tools.ops.backends.factory import build_ops_backend, ops_capability_report
from mewcode.tools.ops.tools import register_ops_tools


@dataclass
class ToolsetAssembly:
    """装配结果。`prompt_note` 是要注入 system prompt 的说明。"""

    tool_names: list[str] = field(default_factory=list)
    ops: Any = None
    prompt_note: str = ""


def assemble_toolset(
    registry: Any,
    config: ToolsetConfig,
    *,
    agent: Any = None,
    work_dir: str | Path = ".",
) -> ToolsetAssembly:
    """按配置把运维工具和 CreatePR 注册进 `registry`。

    没有配置 `toolset` 时什么都不做 —— **默认行为不变**，
    不会为了新功能改变所有现有用户的工具集。
    """
    result = ToolsetAssembly()
    work_dir = Path(work_dir).resolve()

    # --- 运维工具 ---
    if config.ops:
        backend = build_ops_backend(
            [
                {
                    "kind": o.kind,
                    "capability": o.capability,
                    "base_url": o.base_url,
                    "token_env": o.token_env,
                    "token": o.token,
                    "timeout": o.timeout,
                }
                for o in config.ops
            ]
        )
        result.ops = backend
        result.tool_names.extend(register_ops_tools(registry, backend))

        # 把能力清单交给模型。这一步不做的话，模型只能靠"撞一次错"
        # 才知道某个数据源没接 —— 而更糟的情况是它把"没接入"当成"没有异常"。
        report = ops_capability_report(backend)
        if report:
            result.prompt_note = report
            # 只有在**确实存在**未接入的能力时才附这条警告。
            # 无差别地附上它，会让模型怀疑一个其实完整的数据源。
            if "✗" in report:
                result.prompt_note += (
                    "\n标注 ✗ 的能力**没有接入数据源**，调用会直接报错。\n"
                    "这不代表「那方面没有问题」—— 不要把「查不到」写成「没有」。"
                )

    # --- CreatePR ---
    if config.create_pr.enabled:
        from mewcode.tools.agent_verify import make_subagent_verifier
        from mewcode.tools.create_pr import CreatePRTool

        artifacts = Path(config.create_pr.artifacts_dir)
        if not artifacts.is_absolute():
            artifacts = work_dir / artifacts

        verifier_runner = None
        if agent is not None and config.create_pr.require_independent:
            verifier_runner = make_subagent_verifier(agent)

        tool = CreatePRTool(
            work_dir=str(work_dir),
            verify_command=config.create_pr.verify_command,
            artifacts_dir=str(artifacts),
            base_ref=config.create_pr.base_ref,
            timeout=int(config.create_pr.timeout),
            verifier_runner=verifier_runner,
            require_independent=config.create_pr.require_independent,
            mode=config.create_pr.mode,
            remote=config.create_pr.remote,
            api_base=config.create_pr.api_base,
        )
        registry.register(tool)
        result.tool_names.append(tool.name)

    return result

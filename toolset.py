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
    knowledge: Any = None
    plugins: Any = None
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
    # 三段（插件 / 运维 / 知识库）都要往 prompt 里写能力清单。
    # **必须累积再统一拼接** —— 早先运维那段写的是 `result.prompt_note = report`（赋值），
    # 会把它前面插件写的说明整段覆盖掉。这种错很隐蔽：三段各自都对，
    # 只有同时配了两段以上才暴露。
    notes: list[str] = []

    # --- 插件（第三方装包即接入私有工具）---
    #
    # 放在最前面：插件注册的是"基础工具"，后面的运维/知识库可能想覆盖同名工具，
    # 让后来者赢更符合直觉（后注册的覆盖先注册的）。
    if config.plugins:
        from mewcode.tools.plugins import load_plugin_tools

        plugin_result = load_plugin_tools(registry, enabled=True)
        result.plugins = plugin_result
        result.tool_names.extend(plugin_result.tools)
        if note := plugin_result.describe():
            notes.append(note)

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
            # 只有在**确实存在**未接入的能力时才附这条警告。
            # 无差别地附上它，会让模型怀疑一个其实完整的数据源。
            if "✗" in report:
                report += (
                    "\n标注 ✗ 的能力**没有接入数据源**，调用会直接报错。\n"
                    "这不代表「那方面没有问题」—— 不要把「查不到」写成「没有」。"
                )
            notes.append(report)

    # --- 知识库（企业规范 / SOP / 架构说明）---
    #
    # 只在**配置了**才注册 —— 和 ops / CreatePR 保持一致：
    # 不配置的人看到的工具集和以前完全一样。
    if config.knowledge is not None:
        from mewcode.tools.knowledge.backends.factory import (
            build_knowledge_backend,
            knowledge_report,
        )
        from mewcode.tools.knowledge.tools import register_knowledge_tools

        kb = build_knowledge_backend(
            {
                "kind": config.knowledge.kind,
                "extra_dirs": config.knowledge.extra_dirs,
                "include_user_dir": config.knowledge.include_user_dir,
                "base_url": config.knowledge.base_url,
                "path": config.knowledge.path,
                "query_param": config.knowledge.query_param,
                "limit_param": config.knowledge.limit_param,
                "read_path": config.knowledge.read_path,
                "read_id_field": config.knowledge.read_id_field,
                "token_env": config.knowledge.token_env,
                "token": config.knowledge.token,
                "timeout": config.knowledge.timeout,
                "trust_env": config.knowledge.trust_env,
                "mapping": config.knowledge.mapping,
                "name": config.knowledge.name,
            },
            work_dir=str(work_dir),
        )
        result.knowledge = kb
        result.tool_names.extend(register_knowledge_tools(registry, kb))

        kb_report = knowledge_report(kb)
        if kb_report:
            # 知识库清单 + 一条硬性提醒。
            # 这条提醒不是客套：没有它，agent 会凭通用最佳实践写代码，
            # 而「通用最佳实践」和「内部规范」经常是冲突的。
            notes.append(
                kb_report
                + "\n**动手改代码之前先 SearchKnowledge 查一次内部规范。**"
                "\n没查就写，很可能违反内部约定而自己察觉不到。"
            )

    # --- 环境管理（平台给定的起/停环境命令）---
    #
    # 这是对方向二那句「自己起测试环境验证」的**安全实现**：
    # agent 能起环境，但只能执行平台定义好的那几条命令 ——
    # 因为给了 docker 权限就等于挂 docker.sock，等于交出宿主 root。
    if config.environment:
        from mewcode.tools.environment import (
            describe_environment,
            register_env_tools,
        )

        result.tool_names.extend(
            register_env_tools(registry, config.environment, str(work_dir))
        )
        if note := describe_environment(config.environment):
            notes.append(note)

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
            standards=config.create_pr.standards,
        )
        registry.register(tool)
        result.tool_names.append(tool.name)

    # 三段的能力清单在这里统一拼接（见函数开头的 notes 说明）
    result.prompt_note = "\n\n".join(notes)
    return result

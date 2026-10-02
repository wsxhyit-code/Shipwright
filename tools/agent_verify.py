"""独立验证子 agent：让"验证通过"由第三方说了算，而不是实现者自己说。

## 为什么需要这一层

`CreatePRTool` 里的命令验证（看退出码）只解决了"测试过没过"。但有一个漏洞：

    实现 agent 自己跑测试 → 自己宣布"过了" → 提 PR

这正是 `teams/coordinator.py:134` 早就写死的那条规则要避免的：

    **NEVER let the implementation worker verify its own work.**

为什么？因为实现者**锚定在自己的方案上**。它会倾向于：
  - 只跑"能过的那几个测试"
  - 看到漂亮的 UI 或通过的测试套件就放行，不注意"一半按钮没功能"
  - 把类型检查的报错当作"无关"忽略掉

独立验证者没有这些锚定——它**看不到实现过程的对话**，只有一份只读工作区加一个任务：
**尝试打破它**。

## 独立性由什么保证（机械保证，不是靠提示词）

| 维度 | 保证方式 |
|---|---|
| **上下文独立** | 全新 `ConversationManager()`，看不到实现对话 |
| **工具只读** | 注册表里只放只读工具，`WriteFile`/`EditFile`/`Bash` 根本不存在 |
| **结论必须显式** | 必须输出 `VERDICT: PASS` 或 `VERDICT: FAIL` |
| **没结论 = 不通过** | 解析不到 VERDICT 时按 FAIL 处理（fail-closed） |
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from mewcode.tools import ToolRegistry

#: 验证子 agent 允许使用的工具 —— 全是只读的。
#: 注意这里没有 Bash：允许它跑测试和允许它改环境是两回事，
#: 需要跑测试的话应该由 `CreatePRTool` 的命令验证那一层负责。
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "ReadFile",
        "Grep",
        "Glob",
        "ToolSearch",
        "TaskGet",
        "TaskList",
        "AskUserQuestion",
    }
)

_VERDICT_RE = re.compile(r"VERDICT\s*[:：]\s*(PASS|FAIL)", re.IGNORECASE)

#: 默认的验证提示词。
#: 关键是最后一句——要求它**找茬**而不是**确认**。只要求"确认对不对"时，
#: 模型倾向于给你想要的答案。
VERIFY_PROMPT_TEMPLATE = """\
你是独立的代码验证者。你看不到实现这个改动的对话，这是刻意的：
你没有锚定在任何方案上，你的任务不是确认它做对了，而是**尝试打破它**。

## 改动内容
{description}

## 已知的失败模式（请对照检查）
1. **验证回路**：你只读代码、描述"我会测什么"、写下 PASS，然后走开。
   必须真的去读实现、追调用链、找出反例。
2. **被前 80% 迷惑**：看到漂亮的实现或通过的测试就倾向放行，
   没注意到边界情况。**前 80% 是容易的部分，你的全部价值在最后 20%。**
3. **把报错当无关**：发现异常时不要急着归因于"环境问题"。

## 你要做的
- 读改动的代码，追一遍调用链，看有没有漏掉的调用点
- 找边界：空输入、超长输入、并发、失败重试、回滚路径
- 看有没有"改了但没改干净"的地方（同类问题只修了一处）
- 如果工作区里有测试，检查它是否真的覆盖了这个改动

## 输出格式（必须严格遵守）
先写你的分析，最后一行必须是：

    VERDICT: PASS

或者

    VERDICT: FAIL

    （FAIL 时在上面写清楚：哪里有问题、什么条件下会出错）
"""


@dataclass
class Verdict:
    passed: bool
    raw: str
    reason: str


def parse_verdict(text: str) -> Verdict:
    """从验证者的输出里解析结论。

    **fail-closed**：解析不到 `VERDICT:` 就按不通过处理。
    否则一个"忘了给结论"的验证者会被当成放行。
    """
    m = _VERDICT_RE.search(text or "")
    if m is None:
        return Verdict(
            passed=False,
            raw=text or "",
            reason="验证者没有给出 VERDICT 结论（按不通过处理）",
        )
    passed = m.group(1).upper() == "PASS"
    # 保留结论之前的分析作为理由
    head = (text or "")[: m.start()].strip()
    reason = head[-1500:] if head else ("验证通过" if passed else "验证未通过")
    return Verdict(passed=passed, raw=text or "", reason=reason)


def build_readonly_registry(
    parent: ToolRegistry,
    allowed: Iterable[str] = READ_ONLY_TOOL_NAMES,
) -> ToolRegistry:
    """从父注册表里挑出**只读工具**，组成一个新注册表。

    验证者拿到的是一个**能力受限**的注册表：写工具压根不在里面，
    所以"验证者顺手把代码改了"这件事在物理上不可能发生。
    """
    allow = set(allowed)
    ro = ToolRegistry()
    for tool in parent.list_tools():
        if tool.name in allow:
            ro.register(tool)
    return ro


#: VerifierRunner 的签名：给一段改动描述，返回验证者的原始输出
VerifierRunner = Callable[[str], Awaitable[str]]


def make_subagent_verifier(
    parent_agent: Any,
    *,
    allowed_tools: Iterable[str] = READ_ONLY_TOOL_NAMES,
    max_iterations: int = 30,
) -> VerifierRunner:
    """用项目现成的 Agent 机制造一个"独立验证者"。

    它和父 agent 共享 client / 协议 / 工作目录，但：

      - 全新对话（独立上下文）
      - 只读注册表（独立工具集）
      - 权限模式 `plan`（即使注册表里有写工具也跑不了）
    """

    async def _run(description: str) -> str:
        from mewcode.agent import Agent as AgentClass
        from mewcode.conversation import ConversationManager
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )

        parent = parent_agent
        registry = build_readonly_registry(parent.registry, allowed_tools)

        # 双重保险：即使注册表被绕过，plan 模式也不放行写操作
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(getattr(parent, "work_dir", ".")),
            rule_engine=RuleEngine(),
            mode=PermissionMode.PLAN,
        )

        verifier = AgentClass(
            client=parent.client,
            registry=registry,
            protocol=parent.protocol,
            work_dir=parent.work_dir,
            max_iterations=max_iterations,
            permission_checker=checker,
            context_window=getattr(parent, "context_window", 200_000),
            instructions_content="你是独立代码验证者。只读，只找问题。",
            hook_engine=getattr(parent, "hook_engine", None),
        )
        # 独立上下文：全新对话，看不到实现过程
        return await verifier.run_to_completion(
            VERIFY_PROMPT_TEMPLATE.format(description=description or "(未提供描述)"),
            ConversationManager(),
        )

    return _run


def make_static_verifier(text: str) -> VerifierRunner:
    """返回固定输出的假验证者，用于测试与本地演练。"""

    async def _run(_description: str) -> str:
        return text

    return _run

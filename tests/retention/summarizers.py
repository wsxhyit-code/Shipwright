"""摘要器。

**重要区分**：这里的 `naive_summary` / `structured_summary` 是**流程夹具**，
不是测量工具。它们用来把整条 pipeline 跑通、把 4 变体隔离和判分逻辑验证掉，
成本为零且完全确定。

**真实的保留率必须用 LLM 跑 `RealSummarizer`**（`-m llm`）——伪造的摘要器是
"我"写的模板，用它得出的数字是自证，没有意义。

两者都通过 `FakeLLMClient` 驱动**真实的 `auto_compact`**：阈值绕过、keep 窗口切分、
`build_compact_messages`、`build_recovery_attachment`、`replace_history`
全部走线上代码，只有 LLM 那一次输出被替换掉。
"""
from __future__ import annotations

from typing import Any, Callable

from mewcode.conversation import Message


class FakeLLMClient:
    """只伪造 LLM 输出，其余一切走真实代码。

    `auto_compact` 对 client 的要求极低（`context/manager.py:794-802`）：
    调用 `client.stream(conv, system=...)` 并从中读取 `TextDelta.text` 累加即可。
    所以这里只需要 yield 一个把摘要包进 `<summary>` 标签的 TextDelta。
    """

    def __init__(self, strategy: Callable[[list[Message]], str]) -> None:
        self._strategy = strategy
        self.seen: list[list[Message]] = []

    async def stream(
        self,
        conversation: Any,
        system: str = "",
        tools: Any = None,
    ):
        from mewcode.tools.base import StreamEnd, TextDelta

        history = list(getattr(conversation, "history", []))
        self.seen.append(history)
        body = self._strategy(history)
        # 刻意按真实模板的形状输出：<analysis> 会被 extract_summary 丢掉
        yield TextDelta(
            text=f"<analysis>\n梳理见下。\n</analysis>\n<summary>\n{body}\n</summary>"
        )
        yield StreamEnd(stop_reason="end_turn")

    def set_max_output_tokens(self, n: int) -> None:  # pragma: no cover - 接口占位
        pass


# 摘要器收到的是 summary_conv.history，里面混着两条提示词消息
# （首条 SUMMARY_PROMPT 和末条"请根据以上对话生成结构化摘要"），必须滤掉，
# 否则它们会被当成"用户原话"原文抄进摘要里。
_PROMPT_MARKERS = ("摘要助手", "生成结构化摘要")


def _conversation_turns(messages: list[Message]) -> list[Message]:
    return [
        m
        for m in messages
        if not any(marker in m.content for marker in _PROMPT_MARKERS)
    ]


def naive_summary(messages: list[Message]) -> str:
    """模拟「旧模板」：9 个栏目都写了，但全是概括，一个具体值都不留。

    这正是 17% 那类缺陷的形状——摘要看起来结构完整、读起来通顺，
    但端口号、文件名、函数名、约束全部在"技术概念""问题解决过程"里被抽象掉了。
    """
    messages = _conversation_turns(messages)
    user_turns = [m for m in messages if m.role == "user" and m.content.strip()]
    return "\n".join(
        [
            "1. 主要请求和意图：用户希望改进并排查当前模块存在的问题。",
            "2. 关键技术概念：分层结构、缓存策略、日志收敛、错误处理归类。",
            f"3. 文件和代码段：讨论涉及若干源文件，共 {len(user_turns)} 轮相关交流。",
            "4. 错误和修复：记录了几处问题，并给出了处理方向。",
            "5. 问题解决过程：先梳理整体结构，再逐项定位与处理。",
            "6. 所有用户消息：（内容从略）",
            "7. 待办任务：按讨论继续推进剩余事项。",
            "8. 当前工作：正在梳理模块结构与依赖方向。",
            "9. 可能的下一步：补充测试覆盖与监控埋点。",
        ]
    )


def structured_summary(messages: list[Message]) -> str:
    """模拟「新模板」：第 6 条强制**所有用户消息原文保留、不可改写**。

    注意这里**刻意只对 user 消息做原文保留**——因为真实模板的
    `SUMMARY_PROMPT` 第 6 条写的就是"所有用户消息"。所以埋在 assistant 消息里的
    探针（tool_args 类）不会被这条兜住。

    这不是 bug，而是这份伪造摘要器**忠实反映模板文本**的结果，也正好说明
    评测套件能诊断出「模板仍然存在的缺口」。
    """
    messages = _conversation_turns(messages)
    user_turns = [m for m in messages if m.role == "user" and m.content.strip()]
    lines = [
        "1. 主要请求和意图：用户希望改进并排查当前模块存在的问题。",
        "2. 关键技术概念：分层结构、缓存策略、日志收敛、错误处理归类。",
        "3. 文件和代码段：讨论涉及若干源文件（细节见第 6 节原话）。",
        "4. 错误和修复：记录了几处问题，并给出了处理方向。",
        "5. 问题解决过程：先梳理整体结构，再逐项定位与处理。",
        "6. 所有用户消息（原文保留，不可改写）：",
    ]
    lines.extend(f"   - {m.content}" for m in user_turns)
    lines.extend(
        [
            "7. 待办任务：见第 6 节用户原话中的未完成项。",
            "8. 当前工作：正在梳理模块结构与依赖方向。",
            "9. 可能的下一步：补充测试覆盖与监控埋点。",
        ]
    )
    return "\n".join(lines)


class FakeSummarizer:
    """把一个策略函数包装成「摘要器」，供 harness 统一调用。"""

    def __init__(self, strategy: Callable[[list[Message]], str], name: str) -> None:
        self._strategy = strategy
        self.name = name

    def make_client(self) -> FakeLLMClient:
        return FakeLLMClient(self._strategy)


class RealSummarizer:
    """真实 LLM 摘要器：端到端走 `auto_compact` + 真实 client。

    只有需要花 API 钱的用例才用它（pytest -m llm）。
    """

    def __init__(self, client: Any, name: str = "real") -> None:
        self._client = client
        self.name = name

    def make_client(self) -> Any:
        return self._client

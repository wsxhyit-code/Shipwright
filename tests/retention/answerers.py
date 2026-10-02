"""答题器：给定"压缩后的上下文"，回答探针问题。

**评测的本质就是这一步**：压缩完再问一遍，看还答不答得对。

`LexicalAnswerer` 是零成本、确定性的字面检索器，给出的是"字符串是否还在"的
精确下界——字面都不在上下文里了，任何模型都不可能回忆起来。真实保留率请用
`LLMAnswerer`（pytest -m llm）。
"""
from __future__ import annotations

from typing import Any, Protocol

from mewcode.conversation import Message

from tests.retention.schema import Probe

# 表示"上下文里找不到"的统一回答。判分器按这个判定 absent 类探针。
ABSENT = "（上下文中未提及）"


class Answerer(Protocol):
    name: str

    async def answer(self, context: list[Message], probe: Probe) -> str: ...


def flatten(messages: list[Message]) -> str:
    """把上下文拍平成一段文本，用于字面检索。

    刻意**不包含探针的 question**——question 由 harness 单独持有，
    否则"问题里出现答案"会让判分自我实现。
    """
    parts: list[str] = []
    for m in messages:
        parts.append(m.content)
        for tu in m.tool_uses:
            parts.append(f"{tu.tool_name} {tu.arguments}")
        for tr in m.tool_results:
            parts.append(tr.content)
    return "\n".join(parts)


class LexicalAnswerer:
    """字面检索：上下文里**字面出现**才答对。

    它认识 `probe.expected`（这是个 oracle），所以只会返回「精确命中」或
    「未提及」两种结果。这样得到的是保留率的**下界**：命中了不代表模型一定能
    用上，但没命中就一定用不上。
    """

    name = "lexical"

    async def answer(self, context: list[Message], probe: Probe) -> str:
        if probe.trap or not probe.expected:
            # 陷阱探针：绝不编造，这是本答题器的正确行为
            return ABSENT
        body = flatten(context)
        return probe.expected if probe.expected in body else ABSENT


class AbsentAnswerer:
    """永远回答"未提及"。

    作为健全性基线：所有非陷阱探针都应判失败、所有陷阱探针都应判通过。
    任何偏离都说明判分器有问题。
    """

    name = "absent"

    async def answer(self, context: list[Message], probe: Probe) -> str:
        return ABSENT


class LLMAnswerer:
    """真实 LLM 答题器：把压缩后的上下文 + 问题发给模型。

    `tools=[]` 是**必须**的。`build_compact_messages` 会主动告诉模型
    "需要细节请用 ReadFile 读取完整会话记录"（`context/manager.py:444`），
    不禁用工具的话模型会去回读原文，把摘要的真实缺陷完全掩盖掉——
    测出来的 100% 是假的。
    """

    name = "llm"

    def __init__(self, client: Any, system: str = "") -> None:
        self._client = client
        self._system = system

    async def answer(self, context: list[Message], probe: Probe) -> str:
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd, TextDelta

        conv = ConversationManager(
            history=[
                *context,
                Message(
                    role="user",
                    content=(
                        f"{probe.question}\n\n"
                        "只根据上面的上下文回答。如果上下文中没有相关信息，"
                        f"就原样回答：{ABSENT}"
                    ),
                ),
            ]
        )
        collected = ""
        async for event in self._client.stream(
            conv, system=self._system, tools=[]  # ← 禁用工具，断掉回读原文的后路
        ):
            if isinstance(event, TextDelta):
                collected += event.text
            elif isinstance(event, StreamEnd):
                pass
        return collected.strip()

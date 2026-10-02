"""三级判分。

判分最容易出的错是**自我实现**：如果 question 里出现了答案，或者判分只做
"答案非空"的检查，保留率就会虚高。所以：

  - generator.self_check 已经保证 expected 不出现在 question 里；
  - 这里对 `absent` 类探针做**反向**判定（答出具体值 = 幻觉 = 失败），
    这样"什么都肯答"的摘要不会显得保留率很高。
"""
from __future__ import annotations

import re
from typing import Callable

from mewcode.conversation import Message  # noqa: F401  (类型提示用)

from tests.retention.answerers import ABSENT
from tests.retention.schema import Probe

# 表示"上下文里没有"的说法。模型用自己的措辞表达 absence 时也算正确。
_ABSENCE_MARKERS = (
    "未提及", "没有提到", "没有提及", "未说明", "没有说明",
    "不知道", "未给出", "没有给出", "无法确定", "没有相关",
    "not mentioned", "no information", "unknown", "not specified",
    "n/a",
)


def normalize(text: str) -> str:
    """归一化：去掉 markdown 装饰、折叠空白、统一小写。

    只做这些——**不做同义词替换**，因为探针事实是伪造的唯一值，
    精确子串匹配才是可信的判据。
    """
    s = text.replace("`", "").replace("**", "").replace("*", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def looks_absent(answer: str) -> bool:
    n = normalize(answer)
    if not n:
        return True
    return any(marker in n for marker in _ABSENCE_MARKERS)


def grade(
    probe: Probe,
    answer: str,
    judge: Callable[[Probe, str], bool] | None = None,
) -> bool:
    """判定一个探针是否被保留。"""
    # 陷阱探针反向判：答出具体值（而不是"未提及"）即为幻觉 → 失败
    if probe.grading == "absent":
        return looks_absent(answer)

    # 非陷阱探针：明确表示"未提及"一律算丢失
    if looks_absent(answer):
        return False

    if probe.grading == "llm_judge" and judge is not None:
        return judge(probe, answer)

    # exact / keyword / (无 judge 的 llm_judge) 都走精确子串匹配
    return normalize(probe.expected) in normalize(answer)


def make_llm_judge(client, system: str = "") -> Callable[[Probe, str], bool]:
    """返回一个同步 judge，用于 rationale 一类需要语义等价的探针。

    用 `asyncio.run` 包异步调用，仅用于同步判分上下文；真实 LLM 评测里
    建议直接用 `grade_async`。
    """
    import asyncio

    def _judge(probe: Probe, answer: str) -> bool:
        return asyncio.run(_judge_async(client, system, probe, answer))

    return _judge


async def _judge_async(client, system: str, probe: Probe, answer: str) -> bool:
    from mewcode.conversation import ConversationManager, Message as Msg
    from mewcode.tools.base import StreamEnd, TextDelta

    conv = ConversationManager(
        history=[
            Msg(
                role="user",
                content=(
                    "判断下面的回答是否表达了同一个意思。\n"
                    f"参考答案：{probe.expected}\n"
                    f"待判回答：{answer}\n"
                    "只输出 YES 或 NO。"
                ),
            )
        ]
    )
    buf = ""
    async for ev in client.stream(conv, system=system, tools=[]):
        if isinstance(ev, TextDelta):
            buf += ev.text
        elif isinstance(ev, StreamEnd):
            pass
    return "yes" in normalize(buf)

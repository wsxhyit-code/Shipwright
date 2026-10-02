"""上下文信息保留评测：共享数据结构。

设计要点（见 README 里「为什么这样构造」）：
  - 事实一律使用**伪造且唯一**的值，避免模型靠先验知识蒙对造成假阳性；
  - 一个探针只承载一个事实，保证丢失时能归因；
  - 探针按 zone 分区（prefix / keep），用于区分「摘要丢了」和「本来就不归摘要管」。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from mewcode.conversation import Message, estimate_tokens

# 7 类在压缩下丢失概率差异极大的信息。分类型报告比单一总分可信得多。
PROBE_TYPES: tuple[str, ...] = (
    "number",       # 数字/常量：端口、超时、行号、版本
    "negation",     # 否定/约束：不要动 X、禁止 Y
    "identifier",   # 标识符：文件名、函数名、commit
    "rationale",    # 决策与理由：选了 A 因为 B
    "todo",         # 未完成项：TODO、待确认
    "tool_args",    # 工具调用参数：命令、路径
    "wording",      # 用户原始措辞：我要的是 X 不是 Y
)

PROBE_TYPE_LABELS: dict[str, str] = {
    "number": "数字/常量",
    "negation": "否定/约束",
    "identifier": "标识符",
    "rationale": "决策与理由",
    "todo": "未完成项",
    "tool_args": "工具调用参数",
    "wording": "用户原始措辞",
}

Zone = Literal["prefix", "keep"]
Grading = Literal["exact", "keyword", "llm_judge", "absent"]


@dataclass
class Probe:
    """一条"必须存活的事实的"探测定义。"""

    probe_id: str
    type: str
    fact: str            # 埋在对话里的原句（用于定位/调试）
    question: str        # 压缩后要问的问题
    expected: str        # 标准答案
    grading: Grading = "exact"
    zone: Zone = "prefix"
    # 陷阱探针：问一个原文根本没有的信息，正确答案是"未提及"。
    # 没有它，一个"什么都肯答"的摘要会显得保留率超高。
    trap: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Probe:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ProbeCase:
    """一条评测用例 = 一段对话 + 埋在里面的探针。"""

    case_id: str
    target_tokens: int
    messages: list[Message] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return estimate_tokens(self.messages)

    def probes_of(self, probe_type: str) -> list[Probe]:
        return [p for p in self.probes if p.type == probe_type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "target_tokens": self.target_tokens,
            # 用例对话只用到 role/content，序列化时保持精简且可读
            "messages": [
                {"role": m.role, "content": m.content} for m in self.messages
            ],
            "probes": [p.to_dict() for p in self.probes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProbeCase:
        return cls(
            case_id=data["case_id"],
            target_tokens=data["target_tokens"],
            messages=[
                Message(role=m["role"], content=m["content"])
                for m in data["messages"]
            ],
            probes=[Probe.from_dict(p) for p in data["probes"]],
        )

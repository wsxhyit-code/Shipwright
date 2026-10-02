"""7 类探针的半结构化填充模板。

"半填充"的意思是：句子骨架固定，只有事实值由 values.py 决定论地填进来。
这样事实唯一、位置精确可控、类型精确可控，且生成零成本（不需要调 LLM）。

约束：**expected 绝不能出现在 question 里**——否则句子级检索会自我实现，
判分失去意义。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from tests.retention import values as V


@dataclass
class ProbeSpec:
    type: str
    fact: str            # 埋进对话的原句；陷阱探针为空串（它本来就不埋事实）
    question: str
    expected: str
    grading: str
    role: str = "user"   # 这条事实以谁的口吻出现


def _number(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    svc = V.fake_service(case_id, str(idx))
    port = V.fake_port(case_id, str(idx))
    shared["svc"] = svc
    return ProbeSpec(
        type="number",
        fact=f"我把 {svc} 的监听端口从默认值改成了 {port}，记得同步一下 nginx 那边。",
        question=f"{svc} 这个服务实际监听在哪个端口？",
        expected=port,
        grading="exact",
    )


def _negation(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    f = V.fake_file(case_id, str(idx))
    return ProbeSpec(
        type="negation",
        fact=f"{f} 是代码生成器产出的，任何情况下都不要手动改它——下次重新生成会被覆盖掉。",
        question="哪个文件是绝对不能手动修改的？",
        expected=f,
        grading="exact",
    )


def _identifier(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    ident = V.fake_ident(case_id, str(idx))
    f = V.fake_file(case_id, str(idx) + "b")
    line = V.fake_line(case_id, str(idx))
    return ProbeSpec(
        type="identifier",
        fact=f"真正的修复点在 {ident}()，就在 {f} 第 {line} 行附近。",
        question="需要修复的那个函数叫什么名字？",
        expected=ident,
        grading="exact",
    )


def _rationale(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    opt = V.fake_option(case_id, str(idx))
    reason = V.fake_reason(case_id, str(idx))
    return ProbeSpec(
        type="rationale",
        fact=f"最终选了 {opt} 这个方案，因为它避开了 {reason} 的问题。",
        question=f"最后为什么选定了 {opt}？",
        expected=reason,
        grading="keyword",
    )


def _todo(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    ident = V.fake_ident(case_id, str(idx))
    return ProbeSpec(
        type="todo",
        fact=f"这个先放着，{ident} 的清理工作回头再处理，现在别动它。",
        question="还有哪件事是明确说了没做完、要留到后面的？",
        expected=ident,
        grading="keyword",
    )


def _tool_args(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    ident = V.fake_ident(case_id, str(idx))
    cmd = f"pytest -k {ident} --tb=short -x"
    return ProbeSpec(
        type="tool_args",
        fact=f"我跑一下这条命令看结果：{cmd}",
        question="之前那条 pytest 命令里，-k 过滤用的关键字是什么？",
        expected=ident,
        grading="exact",
        role="assistant",
    )


def _wording(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    x = V.fake_ident(case_id, f"{idx}x")
    y = V.fake_ident(case_id, f"{idx}y")
    return ProbeSpec(
        type="wording",
        fact=f"我要的是 {x} 那条路径，不是 {y}，别弄混了。",
        question="用户明确说他要的是哪一条路径？",
        expected=x,
        grading="exact",
    )


def _trap(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    """陷阱探针：问一个原文根本没有的细节，正确答案是"未提及"。

    没有它，"什么都肯答"的摘要会显得保留率超高。
    """
    svc = shared.get("svc") or V.fake_service(case_id, "trap")
    return ProbeSpec(
        type="trap",
        fact="",  # 刻意不埋
        question=f"{svc} 的并发上限设成了多少？",
        expected="",
        grading="absent",
    )


_SPEC_BUILDERS: dict[str, Callable[[str, int, dict[str, Any]], ProbeSpec]] = {
    "number": _number,
    "negation": _negation,
    "identifier": _identifier,
    "rationale": _rationale,
    "todo": _todo,
    "tool_args": _tool_args,
    "wording": _wording,
}


def build_probe_spec(
    probe_type: str, case_id: str, idx: int, shared: dict[str, Any]
) -> ProbeSpec:
    return _SPEC_BUILDERS[probe_type](case_id, idx, shared)


def build_trap_spec(case_id: str, idx: int, shared: dict[str, Any]) -> ProbeSpec:
    return _trap(case_id, idx, shared)

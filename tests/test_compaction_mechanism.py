"""压缩机制不变量测试（零成本，伪造 LLM）。

这些用例保护的是 `auto_compact` **本身**，不是保留率：

  M1  压缩真的触发（而不是被 min-prefix 阈值静默跳过）
  M2  净省 token
  M3  keep 尾部与原文**同一对象**（原样保留，一个字都没改）
  M4  新 history 恰好 = [摘要消息] + keep
  M5  keep 里不出现孤立的 tool_result（会导致 API 报错）
  M6  <analysis> 已被剥离
  M7  被摘要的前缀真的不在 history 里了
  M8  token 账目对得上（before == prefix + keep）
  M9  keep 窗口遵守常量约束

真实压缩比见 `python -m tests.retention.audit --real`。
"""
from __future__ import annotations

import pytest

from mewcode.conversation import ConversationManager, Message
from mewcode.context.manager import auto_compact
from tests.retention.audit import _has_isolated_tool_result, _role_token_share, audit_case
from tests.retention.generator import (
    REALISTIC_MIX,
    build_case,
)
from tests.retention.summarizers import FakeSummarizer, structured_summary

FAKE = FakeSummarizer(structured_summary, "structured")


# ---------------------------------------------------------------------------
# 数据集：真实角色分布
# ---------------------------------------------------------------------------


def test_realistic_distribution_matches_real_sessions():
    """真实会话里 tool_result 是大头（约 70%），不是 user。

    依据：本仓库 .mewcode/session/tool-results/ 下 9 个已持久化输出合计 687KB，
    全部来自 tool_result；逐条读过的真实 jsonl 里一条子 agent 报告就占 8KB。
    用 99%-user 的失真数据会让摘要看起来全都在膨胀。
    """
    case = build_case("dist-check", target_tokens=16_000, realistic=True)
    share = _role_token_share(case.messages)
    assert share["tool_result"] > 0.5, f"tool_result 占比过低: {share}"
    assert share["user"] < 0.25, f"user 占比过高: {share}"
    # 与声明的混合比大致吻合
    assert abs(share["tool_result"] - REALISTIC_MIX["tool_result"]) < 0.15


def test_distorted_distribution_is_dominated_by_user():
    """对照：默认数据集确实是失真的（这是已知且有意为之的旧行为）。"""
    case = build_case("distorted-check", target_tokens=8_000)
    share = _role_token_share(case.messages)
    assert share["user"] > 0.9
    assert share["tool_result"] == 0.0


# ---------------------------------------------------------------------------
# 机制不变量
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("realistic", [True, False])
async def test_structural_invariants_hold(realistic, tmp_path):
    """结构类不变量（M1/M3~M9）是**机制的责任**，两种数据分布下都必须成立。

    刻意排除 M2（净省 token）：它取决于**摘要器**而不是机制——
    逐字转储式的摘要在这份 99%-user 的失真数据上会膨胀
    （见 test_retention_pipeline.py::test_verbatim_summary_can_inflate_the_context）。
    M2 由 test_compaction_always_saves_tokens_on_realistic_data 单独守护。
    M6b 是 extract_summary 的已知缺陷（见 test_extract_summary.py）。
    """
    case = build_case(
        f"mech-{'real' if realistic else 'dist'}",
        target_tokens=12_000,
        realistic=realistic,
    )
    rep = await audit_case(case, FAKE.make_client(), tmp_path / "session")

    failed = [
        (name, detail)
        for name, ok, detail in rep.checks
        if not ok and not name.startswith(("M6b", "M2"))
    ]
    assert not failed, f"机制不变量未通过: {failed}"


async def test_keep_tail_is_byte_identical(tmp_path):
    """M3：keep 必须是原文里那几个**同一对象**，不能被复制或改写。"""
    case = build_case("mech-keep-identity", target_tokens=12_000, realistic=True)
    original = list(case.messages)
    conv = ConversationManager(history=list(case.messages))

    event = await auto_compact(
        conv, FAKE.make_client(), 200_000, tmp_path / "s",
        protocol="anthropic", manual=True, transcript_path="",
    )
    keep = list(event.boundary.keep)
    keep_start = len(original) - len(keep)
    assert all(original[keep_start + i] is m for i, m in enumerate(keep))
    # 且新 history 的前 len(keep) 之后就是这些对象
    assert all(conv.history[1 + i] is m for i, m in enumerate(keep))


async def test_prefix_is_actually_discarded(tmp_path):
    """M7：被摘要的前缀不再出现在 history 里——否则压缩是假的。"""
    case = build_case("mech-prefix-drop", target_tokens=12_000, realistic=True)
    original = list(case.messages)
    conv = ConversationManager(history=list(case.messages))
    event = await auto_compact(
        conv, FAKE.make_client(), 200_000, tmp_path / "s",
        protocol="anthropic", manual=True, transcript_path="",
    )
    keep_start = len(original) - len(event.boundary.keep)
    new_ids = {id(m) for m in conv.history}
    assert not any(id(m) in new_ids for m in original[:keep_start])


async def test_no_orphan_tool_result_in_keep(tmp_path):
    """M5：keep 窗口里不许有配不上 tool_use 的 tool_result。

    `_align_keep_start_to_tool_pair` 就是为此存在的：宁可多保留一对，
    也不留下半对——半个 tool_result 会让 API 直接报错。
    """
    # 构造一个"切分点正好落在 tool_result 上"的场景
    case = build_case("mech-pairing", target_tokens=10_000, realistic=True)
    conv = ConversationManager(history=list(case.messages))
    event = await auto_compact(
        conv, FAKE.make_client(), 200_000, tmp_path / "s",
        protocol="anthropic", manual=True, transcript_path="",
    )
    assert not _has_isolated_tool_result(list(event.boundary.keep))


def test_has_isolated_tool_result_detects_the_bad_case():
    """给上面那个断言本身做个反向验证：孤立的 tool_result 必须能被查出来。"""
    from mewcode.conversation import ToolResultBlock

    orphan = [
        Message(role="user", content="", tool_results=[
            ToolResultBlock(tool_use_id="call_x", content="结果")
        ]),
    ]
    assert _has_isolated_tool_result(orphan) == "call_x"
    assert _has_isolated_tool_result([]) == ""


async def test_token_accounting_balances(tmp_path):
    """M8：before 必须能被 prefix + keep 解释清楚，不能有凭空多出/蒸发的 token。"""
    case = build_case("mech-accounting", target_tokens=16_000, realistic=True)
    rep = await audit_case(case, FAKE.make_client(), tmp_path / "s")
    from mewcode.conversation import estimate_tokens

    accounted = rep.prefix_tokens + rep.keep_tokens
    assert abs(accounted - rep.before_tokens) <= max(50, rep.before_tokens * 0.05)


async def test_compaction_always_saves_tokens_on_realistic_data(tmp_path):
    """机制必须在真实分布的数据上也净省 token。

    这条是"压缩比 > 1"那个问题的回归护栏：一旦有人在真实分布下把压缩做成膨胀，
    这里会立刻红。
    """
    case = build_case("mech-saves", target_tokens=16_000, realistic=True)
    rep = await audit_case(case, FAKE.make_client(), tmp_path / "s")
    assert rep.compression_ratio < 0.5, (
        f"真实分布下压缩比 {rep.compression_ratio:.3f}，机制可能退化了"
    )

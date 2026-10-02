"""上下文信息保留评测：pipeline 测试（零 API 成本）。

用伪造摘要器把整条流水线跑通：真实的 `auto_compact`、真实的 keep 窗口切分、
真实的 `build_compact_messages` / `build_recovery_attachment`，只把 LLM 那一次
输出替换掉。因此这些用例验证的是**评测流程本身是否成立**，不是真实保留率。

真实保留率见 `test_retention_llm.py`（pytest -m llm）。
"""
from __future__ import annotations

import pytest

from mewcode.conversation import Message
from tests.retention.answerers import ABSENT, AbsentAnswerer, LexicalAnswerer
from tests.retention.generator import build_case, build_dataset, load_dataset, write_dataset
from tests.retention.graders import grade, looks_absent, normalize
from tests.retention.harness import (
    ALL_VARIANTS,
    build_contexts,
    compute_metrics,
    format_report,
    run_case,
    run_dataset,
)
from tests.retention.schema import PROBE_TYPES, Probe, ProbeCase
from tests.retention.summarizers import (
    FakeSummarizer,
    naive_summary,
    structured_summary,
)

NAIVE = FakeSummarizer(naive_summary, "naive")
STRUCTURED = FakeSummarizer(structured_summary, "structured")


# ---------------------------------------------------------------------------
# 数据集本身
# ---------------------------------------------------------------------------


def test_generator_covers_all_seven_types_plus_trap(prefix_case):
    types = {p.type for p in prefix_case.probes}
    assert set(PROBE_TYPES) <= types
    assert "trap" in types


def test_generator_invariants_hold():
    """generator.self_check 的三条底线：唯一性、不自我实现、事实真的埋进去了。"""
    for case in build_dataset(sizes=(6_000,), cases_per_size=1):
        expected = [p.expected for p in case.probes if p.expected]
        assert len(expected) == len(set(expected)), "期望值重复，无法归因"
        for p in case.probes:
            if p.expected:
                assert p.expected not in p.question, "expected 出现在 question 里"
        body = "\n".join(m.content for m in case.messages)
        for p in case.probes:
            if not p.trap:
                assert body.count(p.expected) == 1, f"{p.probe_id} 埋点异常"


def test_dataset_json_roundtrip(tmp_path):
    cases = build_dataset(sizes=(6_000,), cases_per_size=1)
    path = write_dataset(cases, tmp_path / "dataset.json")
    back = load_dataset(path)
    assert len(back) == len(cases)
    original = {p.probe_id: p.expected for c in cases for p in c.probes}
    restored = {p.probe_id: p.expected for c in back for p in c.probes}
    assert original == restored


# ---------------------------------------------------------------------------
# 4 变体隔离——这是整套评测最容易做错的地方
# ---------------------------------------------------------------------------


def test_variant_isolation():
    """变体的唯一区别必须是"给模型看多少上下文"，不能串味。"""
    case = build_case("iso", target_tokens=6_000, with_trap=False)
    ctxs = build_contexts(
        case,
        "SUMMARY_MARK",
        [Message(role="user", content="KEEP_MARK")],
        [Message(role="user", content="E2E_MARK")],
    )

    # control：原文全给
    assert len(ctxs["control"]) == len(case.messages)

    # summary：只有摘要，既不带 keep，也不带恢复附件
    text = "\n".join(m.content for m in ctxs["summary"])
    assert "SUMMARY_MARK" in text
    assert "KEEP_MARK" not in text
    assert "E2E_MARK" not in text
    # 且**不能**带 transcript 回读提示——否则模型可以绕开摘要去读原文
    assert "ReadFile" not in text

    # keep：摘要 + 尾部原文，仍然不带恢复附件
    ktext = "\n".join(m.content for m in ctxs["keep"])
    assert "SUMMARY_MARK" in ktext and "KEEP_MARK" in ktext
    assert "E2E_MARK" not in ktext

    # e2e：完整机制，什么都不缺
    etext = "\n".join(m.content for m in ctxs["e2e"])
    assert "E2E_MARK" in etext


async def test_compaction_really_triggers(prefix_case, session_dir):
    """最基本的一条：压缩必须真的发生。

    不满足这个，后面所有的"保留率"都是在测一段没被压缩过的原文。
    """
    run = await run_case(prefix_case, NAIVE, LexicalAnswerer(), session_dir)
    assert run.valid, run.invalid_reason
    assert run.after_tokens < run.before_tokens, "没有省下 token"
    assert run.summary.strip(), "摘要为空"
    assert run.keep_count > 0, "keep 窗口为空"


async def test_short_conversation_is_reported_invalid(tmp_path):
    """太短的对话会被 auto_compact 静默跳过——必须被标记为无效，而不是假通过。

    注意不能用 build_case(target_tokens=2000) 来造：generator.self_check 会直接
    拦住"长度不足以触发压缩"的用例（这是故意的）。这里改成截取一条正常用例的尾部，
    让**前缀**小到不值得摘要。
    """
    full = build_case("fx-for-tiny", target_tokens=8_000, with_trap=False)
    tiny = ProbeCase(
        case_id="tiny",
        target_tokens=500,
        messages=full.messages[-6:],  # keep 窗口 5 条 + 1 条极小前缀
        probes=full.probes,
    )
    run = await run_case(tiny, NAIVE, LexicalAnswerer(), tmp_path / "session")
    assert not run.valid
    assert "压缩未触发" in run.invalid_reason


# ---------------------------------------------------------------------------
# 排除法：四个变体各自说明什么
# ---------------------------------------------------------------------------


async def test_control_variant_must_be_100(prefix_case, session_dir):
    """对照组是天花板。它不到 100%，说明探针本身有问题，整份结果不可信。"""
    run = await run_case(prefix_case, NAIVE, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "control")
    assert m.probes > 0
    assert m.irr == 1.0, f"对照组只有 {m.irr:.1%}，探针设计有问题"


async def test_naive_template_loses_every_probe(prefix_case, session_dir):
    """概括型摘要：9 个栏目俱全，但一个具体值都不留 → 摘要变体归零。"""
    run = await run_case(prefix_case, NAIVE, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "summary")
    assert m.irr == 0.0, f"概括型摘要不该保住任何具体值，实得 {m.irr:.1%}"


async def test_structured_template_rescues_user_messages_only(prefix_case, session_dir):
    """新模板第 6 条强制用户原话原文保留 → 用户的 6 类全部保住。

    但 `tool_args` 埋在 **assistant** 消息里，而第 6 条只覆盖"所有用户消息"，
    所以它仍然丢——这是评测套件诊断出的**模板仍然存在的缺口**，不是 bug。
    """
    run = await run_case(prefix_case, STRUCTURED, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "summary")

    assert m.by_type["number"] == 1.0
    assert m.by_type["negation"] == 1.0
    assert m.by_type["identifier"] == 1.0
    assert m.by_type["rationale"] == 1.0
    assert m.by_type["todo"] == 1.0
    assert m.by_type["wording"] == 1.0
    assert m.by_type["tool_args"] == 0.0, "tool_args 在 assistant 消息里，第 6 条管不到"
    assert m.irr == pytest.approx(6 / 7, abs=1e-6)


async def test_keep_zone_probes_survive_only_in_keep_variant(keepzone_case, session_dir):
    """排除法分支：探针在 keep 窗口里 → summary 变体丢、keep 变体保住。

    看到这个组合就说明**探针位置标错了**，不该去改摘要模板。
    """
    run = await run_case(keepzone_case, STRUCTURED, LexicalAnswerer(), session_dir)
    assert run.valid, run.invalid_reason
    assert compute_metrics([run], "summary").irr == 0.0
    assert compute_metrics([run], "keep").irr == 1.0
    assert compute_metrics([run], "e2e").irr == 1.0


async def test_recovery_attachment_appears_in_e2e_only(prefix_case, session_dir):
    """恢复通道（agent.py:940 的快照 → build_recovery_attachment）只作用于 e2e。

    这条用例的意义是：**别把恢复通道的功劳记到摘要模板头上**。
    """
    with_rec = await run_case(
        prefix_case, NAIVE, LexicalAnswerer(), session_dir, with_recovery=True
    )
    without_rec = await run_case(
        prefix_case, NAIVE, LexicalAnswerer(), session_dir, with_recovery=False
    )

    neg = "negation"
    rec_neg = [o for o in with_rec.outcomes if o.probe_type == neg and o.variant == "e2e"]
    plain_neg = [
        o for o in without_rec.outcomes if o.probe_type == neg and o.variant == "e2e"
    ]
    assert rec_neg and rec_neg[0].ok, "带恢复附件时 negation 应该被捞回来"
    assert plain_neg and not plain_neg[0].ok, "不带恢复附件时应该丢失"

    # 关键：summary 变体两条都不该保住——它本来就不含恢复附件
    for run in (with_rec, without_rec):
        s = [o for o in run.outcomes if o.probe_type == neg and o.variant == "summary"]
        assert s and not s[0].ok, "摘要变体不该被恢复附件影响"


# ---------------------------------------------------------------------------
# 指标与判分器
# ---------------------------------------------------------------------------


async def test_trap_probes_are_never_hallucinated_by_lexical(prefix_case, session_dir):
    """字面检索器绝不编造 → 幻觉率必须为 0，且陷阱探针不计入 IRR 分母。"""
    run = await run_case(prefix_case, STRUCTURED, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "summary")
    assert m.hallucination_rate == 0.0
    trap_total = sum(1 for o in run.outcomes if o.variant == "summary" and o.trap)
    assert trap_total == 1
    assert m.probes == run.probe_count - trap_total


async def test_absent_answerer_baseline(prefix_case, session_dir):
    """"永远答未提及"的基线：非陷阱全失败、陷阱全通过。判分器偏离即为 bug。"""
    run = await run_case(prefix_case, STRUCTURED, AbsentAnswerer(), session_dir)
    for variant in ALL_VARIANTS:
        m = compute_metrics([run], variant)
        assert m.irr == 0.0
        assert m.hallucination_rate == 0.0


async def test_compression_ratio_is_reported(prefix_case, session_dir):
    """指标必须是"保留率 × 压缩比"的联合体——单独报保留率可以靠不压缩刷分。"""
    run = await run_case(prefix_case, STRUCTURED, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "summary")
    assert m.compression_ratio > 0.0
    assert run.after_tokens != run.before_tokens
    assert m.info_density > 0


async def test_verbatim_summary_can_inflate_the_context(prefix_case, session_dir):
    """**这套指标设计的存在理由**：逐字转储用户原话的摘要可能比原文还大。

    `structured_summary` 忠实执行模板第 6 条"原文保留，不可改写"，把每条用户消息
    完整抄进摘要——于是"压缩"后的上下文反而膨胀了（压缩比 > 1）。

    这时候保留率再高也没有意义：它是靠**花更多 token** 换来的。
    所以任何单独报 IRR 的结论都必须同时给出压缩比。
    """
    run = await run_case(prefix_case, STRUCTURED, LexicalAnswerer(), session_dir)
    m = compute_metrics([run], "summary")

    assert m.irr > 0.8, "逐字转储确实保住了信息"
    assert m.compression_ratio >= 1.0, (
        "本条用例的预期就是膨胀；若不再膨胀说明摘要策略变了，需要重新审视指标口径"
    )
    # 报告里必须出现膨胀告警
    report = format_report([run], title="膨胀检查")
    assert "膨胀" in report or "偏高" in report


def test_graders_exact_and_keyword():
    p = Probe("p1", "number", "端口 8391", "端口是多少？", "8391", grading="exact")
    assert grade(p, "服务监听在 8391 端口")
    assert grade(p, "端口是 8391。")
    assert not grade(p, "端口是 8390")
    assert not grade(p, "8080")
    assert not grade(p, ABSENT)


def test_graders_absent_is_reverse_scored():
    """陷阱探针反向判：答出具体值就是幻觉。"""
    trap = Probe("t1", "trap", "", "并发上限是多少？", "", grading="absent", trap=True)
    assert grade(trap, ABSENT)
    assert grade(trap, "上下文中没有提到并发上限")
    assert grade(trap, "not mentioned in the context")
    assert not grade(trap, "并发上限是 200")
    assert not grade(trap, "应该是 1000 左右")


def test_normalize_strips_markdown_noise():
    assert normalize("`8391`") == "8391"
    assert normalize("**8391**") == "8391"
    assert normalize("  8391  ") == "8391"
    assert normalize("a\n\nb") == "a b"


def test_looks_absent():
    assert looks_absent("")
    assert looks_absent(ABSENT)
    assert looks_absent("上下文中未提及该信息")
    assert not looks_absent("8391")


async def test_report_renders_with_selfcheck(prefix_case, session_dir):
    runs = await run_dataset([prefix_case], STRUCTURED, LexicalAnswerer(), session_dir)
    report = format_report(runs, title="冒烟")
    assert "summary 仅摘要（诊断模板）" in report
    assert "对照组 100%" in report
    assert "分类型保留率" in report

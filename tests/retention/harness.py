"""评测执行器：4 变体隔离 + 跑批 + 指标汇总。

## 为什么是 4 个变体

同一份信息可能来自 4 个不同的地方，不隔离就分不清是谁的功劳/责任。
4 个变体的唯一区别就是**给模型看多少上下文**：

  control  不压缩，原文全给          → 必须 100%，否则说明探针本身有问题，测试作废
  summary  只有摘要文本              → 这才是「摘要模板的保留率」
  keep     摘要 + keep 尾部原文      → 若这里保住了而 summary 没保住，说明探针埋错区了
  e2e      完整机制（含恢复附件）    → 系统整体

排除法：control 错 → 测试废了；summary 错 keep 对 → 位置标错；summary 错 keep 也错
→ 摘要模板真的丢了它。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from mewcode.conversation import ConversationManager, Message, estimate_tokens
from mewcode.context.manager import (
    MIN_SUMMARIZE_PREFIX_TOKENS,
    CompactEvent,
    RecoveryState,
    _compute_keep_start_index,
    auto_compact,
    build_compact_messages,
)

from tests.retention.answerers import Answerer
from tests.retention.graders import grade
from tests.retention.schema import PROBE_TYPES, ProbeCase

Variant = Literal["control", "summary", "keep", "e2e"]
ALL_VARIANTS: tuple[Variant, ...] = ("control", "summary", "keep", "e2e")

VARIANT_LABELS: dict[str, str] = {
    "control": "control 对照组（不压缩）",
    "summary": "summary 仅摘要（诊断模板）",
    "keep": "keep 摘要+尾部原文",
    "e2e": "e2e 完整机制",
}

CONTEXT_WINDOW = 200_000


@dataclass
class ProbeOutcome:
    case_id: str
    probe_id: str
    probe_type: str
    zone: str
    variant: str
    answer: str
    ok: bool
    trap: bool = False


@dataclass
class CaseRun:
    case_id: str
    before_tokens: int = 0
    after_tokens: int = 0
    summary: str = ""
    summary_tokens: int = 0
    keep_count: int = 0
    probe_count: int = 0
    outcomes: list[ProbeOutcome] = field(default_factory=list)
    valid: bool = True
    invalid_reason: str = ""

    @property
    def compression_ratio(self) -> float:
        if self.before_tokens <= 0:
            return 0.0
        return self.after_tokens / self.before_tokens


@dataclass
class Metrics:
    variant: str
    cases: int
    probes: int
    retained: int
    irr: float
    by_type: dict[str, float]
    by_zone: dict[str, float]
    hallucination_rate: float
    compression_ratio: float
    info_density: float


def _build_recovery(case: ProbeCase) -> RecoveryState:
    """按真实机制构造恢复快照。

    `agent.py:940` 的 `_snapshot_for_recovery` 会把 ReadFile 的内容记进
    `RecoveryState`，压缩后由 `build_recovery_attachment` 重新贴到摘要消息上。
    这里模拟"agent 读过一个文件"这一事实。

    它的意义不是让 e2e 好看，而是让「摘要丢了但恢复机制捞回来了」这种情况
    **可被观测**——否则你会把恢复通道的功劳记到摘要模板头上。
    """
    state = RecoveryState()
    for p in case.probes:
        if p.type == "negation" and p.expected:
            state.record_file_read(
                f"/repo/{p.expected}", f"# {p.expected}\n# 本文件由代码生成器产出，请勿手改\n"
            )
    return state


def build_contexts(
    case: ProbeCase, summary: str, keep: list[Message], e2e: list[Message]
) -> dict[str, list[Message]]:
    """构造 4 个变体的上下文。全部复用线上代码，不手搓消息。"""
    return {
        # 对照组：原文全给，应该 100%
        "control": list(case.messages),
        # 只有摘要：不带 keep、不带恢复附件、不给 transcript 提示
        "summary": build_compact_messages(
            summary, attachment="", has_keep_tail=False, transcript_path=""
        ),
        # 摘要 + keep 尾部原文
        "keep": build_compact_messages(
            summary, attachment="", has_keep_tail=True, transcript_path=""
        )
        + list(keep),
        # 完整机制：直接取 auto_compact 重写后的 history（含恢复附件）
        "e2e": list(e2e),
    }


async def run_case(
    case: ProbeCase,
    summarizer: Any,
    answerer: Answerer,
    session_dir: Path | str,
    *,
    context_window: int = CONTEXT_WINDOW,
    with_recovery: bool = True,
    variants: Sequence[Variant] = ALL_VARIANTS,
) -> CaseRun:
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    conv = ConversationManager(history=list(case.messages))
    before_tokens = conv.current_tokens()
    # 先算一遍切分，这样 auto_compact 不触发时能给出**具体**原因
    keep_start = _compute_keep_start_index(conv.history)

    event = await auto_compact(
        conv,
        summarizer.make_client(),
        context_window,
        session_dir,
        protocol="anthropic",
        # ★ manual=True 跳过阈值检查（context/manager.py:749），
        #   让"何时压缩"完全可控、可复现，而不是等 token 阈值自然触发
        manual=True,
        recovery=_build_recovery(case) if with_recovery else None,
        transcript_path="",
    )

    # ★★ 必须确认压缩真的发生了。`auto_compact` 是**三态返回**：
    #    CompactEvent → 成功
    #    None         → 没触发（keep_start<=0 或前缀 < MIN_SUMMARIZE_PREFIX_TOKENS）
    #    str          → 摘要生成失败（异常被吞成了错误字符串）
    #    三者必须分开报告，否则一次瞬时 API 失败会被误读成"没触发"。
    if not isinstance(event, CompactEvent) or event.boundary is None:
        if event is None:
            reason = (
                f"压缩未触发：keep_start={keep_start}（须 >0）、"
                f"前缀 {estimate_tokens(case.messages[:keep_start])} tokens"
                f"（须 ≥ {MIN_SUMMARIZE_PREFIX_TOKENS}）"
            )
        else:
            reason = f"摘要生成失败（auto_compact 返回 {type(event).__name__}）：{str(event)[:300]}"
        return CaseRun(
            case_id=case.case_id,
            before_tokens=before_tokens,
            probe_count=len(case.probes),
            valid=False,
            invalid_reason=reason,
        )

    summary = event.boundary.summary
    keep = list(event.boundary.keep)
    after_tokens = conv.current_tokens()

    contexts = build_contexts(case, summary, keep, list(conv.history))

    outcomes: list[ProbeOutcome] = []
    for variant in variants:
        ctx = contexts[variant]
        for probe in case.probes:
            answer = await answerer.answer(ctx, probe)
            outcomes.append(
                ProbeOutcome(
                    case_id=case.case_id,
                    probe_id=probe.probe_id,
                    probe_type=probe.type,
                    zone=probe.zone,
                    variant=variant,
                    answer=answer,
                    ok=grade(probe, answer),
                    trap=probe.trap,
                )
            )

    return CaseRun(
        case_id=case.case_id,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        summary=summary,
        summary_tokens=estimate_tokens([Message(role="user", content=summary)]),
        keep_count=len(keep),
        probe_count=len(case.probes),
        outcomes=outcomes,
    )


async def run_dataset(
    cases: Sequence[ProbeCase],
    summarizer: Any,
    answerer: Answerer,
    session_dir: Path | str,
    **kwargs: Any,
) -> list[CaseRun]:
    runs: list[CaseRun] = []
    for case in cases:
        runs.append(await run_case(case, summarizer, answerer, session_dir, **kwargs))
    return runs


def compute_metrics(runs: Sequence[CaseRun], variant: str) -> Metrics:
    """按变体汇总指标。

    关键：`irr` 只统计**非陷阱探针**，陷阱探针单独算 `hallucination_rate`。
    否则"乱答"和"没保留"会被混成一个数。
    """
    real = [
        o
        for r in runs
        if r.valid
        for o in r.outcomes
        if o.variant == variant and not o.trap
    ]
    traps = [
        o
        for r in runs
        if r.valid
        for o in r.outcomes
        if o.variant == variant and o.trap
    ]

    total = len(real)
    retained = sum(1 for o in real if o.ok)

    by_type: dict[str, float] = {}
    for ptype in PROBE_TYPES:
        items = [o for o in real if o.probe_type == ptype]
        if items:
            by_type[ptype] = sum(1 for o in items if o.ok) / len(items)

    by_zone: dict[str, float] = {}
    for zone in ("prefix", "keep"):
        items = [o for o in real if o.zone == zone]
        if items:
            by_zone[zone] = sum(1 for o in items if o.ok) / len(items)

    hallucination_rate = 0.0
    if traps:
        hallucination_rate = sum(1 for o in traps if not o.ok) / len(traps)

    valid_runs = [r for r in runs if r.valid]
    before = sum(r.before_tokens for r in valid_runs)
    after = sum(r.after_tokens for r in valid_runs)
    compression_ratio = (after / before) if before > 0 else 0.0

    summary_tokens = sum(r.summary_tokens for r in valid_runs)
    info_density = (retained / summary_tokens) if summary_tokens > 0 else 0.0

    return Metrics(
        variant=variant,
        cases=len(valid_runs),
        probes=total,
        retained=retained,
        irr=(retained / total) if total else 0.0,
        by_type=by_type,
        by_zone=by_zone,
        hallucination_rate=hallucination_rate,
        compression_ratio=compression_ratio,
        info_density=info_density,
    )


def _bar(value: float, width: int = 20) -> str:
    filled = int(round(value * width))
    return "█" * filled + "·" * (width - filled)


def format_report(runs: Sequence[CaseRun], title: str = "") -> str:
    """打印一张可读的报告。"""
    lines: list[str] = []
    if title:
        lines.append(f"\n=== {title} ===")

    invalid = [r for r in runs if not r.valid]
    if invalid:
        lines.append(f"\n⚠️  {len(invalid)} 条用例无效（压缩未触发），已从指标中剔除：")
        for r in invalid:
            lines.append(f"    - {r.case_id}: {r.invalid_reason}")

    lines.append("\n用例概览：")
    for r in runs:
        if not r.valid:
            continue
        lines.append(
            f"  {r.case_id:16} {r.before_tokens:>7} → {r.after_tokens:>6} tokens "
            f"(压缩比 {r.compression_ratio:.2f})  keep={r.keep_count}  "
            f"摘要={r.summary_tokens} tokens"
        )

    lines.append("\n各变体指标：")
    header = f"  {'变体':<26}{'IRR':>8}  {'探针':>5}  {'压缩比':>7}  {'幻觉率':>7}"
    lines.append(header)
    lines.append("  " + "-" * 60)
    for variant in ALL_VARIANTS:
        m = compute_metrics(runs, variant)
        if m.probes == 0:
            continue
        lines.append(
            f"  {VARIANT_LABELS[variant]:<26}{m.irr:>7.1%}  {m.probes:>5}  "
            f"{m.compression_ratio:>7.2f}  {m.hallucination_rate:>7.1%}"
        )

    summary_m = compute_metrics(runs, "summary")
    control_m = compute_metrics(runs, "control")
    lines.append("\n分类型保留率（summary 变体）：")
    for ptype, value in summary_m.by_type.items():
        lines.append(f"  {ptype:<12} {value:>6.1%}  {_bar(value)}")

    lines.append("\n分区保留率：")
    for zone, value in summary_m.by_zone.items():
        lines.append(f"  {zone:<12} {value:>6.1%}  {_bar(value)}")

    lines.append("\n排除法自检：")
    if control_m.probes and control_m.irr < 1.0:
        lines.append(
            f"  ❌ 对照组只有 {control_m.irr:.1%}，说明探针本身有问题（歧义/先验泄漏），"
            "整份结果不可信"
        )
    else:
        lines.append("  ✅ 对照组 100%，探针有效，下面的数字可信")

    if summary_m.compression_ratio >= 1.0:
        lines.append(
            f"  ❌ 压缩比 {summary_m.compression_ratio:.2f} ≥ 1：摘要比原文还大，"
            "「压缩」实际是膨胀。此时的高保留率是靠烧更多 token 换来的，不可采信。"
        )
    elif summary_m.compression_ratio > 0.9:
        lines.append(
            f"  ⚠️ 压缩比 {summary_m.compression_ratio:.2f} 偏高（几乎没压缩），"
            "此时的高保留率没有意义"
        )
    return "\n".join(lines)

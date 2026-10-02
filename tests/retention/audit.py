"""压缩机制体检：检查 `auto_compact` **本身**对不对（不是测保留率）。

保留率评测回答"信息还在不在"；这个体检回答的是更前面一层的问题：
**压缩这个动作有没有做对**——该保留的尾部有没有原样保留、该丢弃的前缀有没有
真的丢掉、tool_use/tool_result 有没有被拆散、token 账目能不能对上。

    # 零成本：伪造摘要器，只跑机制不变量
    python -m tests.retention.audit --strategy structured

    # 真实压缩比（花钱）
    python -m tests.retention.audit --real --sizes 16

    # 对比默认数据集（99% user，失真）与真实角色分布
    python -m tests.retention.audit --real --sizes 16 --distorted
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mewcode.conversation import ConversationManager, Message, estimate_tokens
from mewcode.context.manager import (
    KEEP_MAX_TOKENS,
    KEEP_RECENT_TOKENS,
    MIN_KEEP_MESSAGES,
    CompactEvent,
    build_compact_messages,
    extract_summary,
    auto_compact,
)

from tests.retention.generator import build_case
from tests.retention.schema import ProbeCase


@dataclass
class MechanismReport:
    case_id: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    before_tokens: int = 0
    after_tokens: int = 0
    prefix_tokens: int = 0
    keep_tokens: int = 0
    summary_tokens: int = 0
    attachment_tokens: int = 0
    keep_start: int = 0
    keep_count: int = 0
    total_messages: int = 0
    role_share: dict[str, float] = field(default_factory=dict)

    @property
    def compression_ratio(self) -> float:
        return self.after_tokens / self.before_tokens if self.before_tokens else 0.0

    @property
    def summary_vs_prefix(self) -> float:
        return self.summary_tokens / self.prefix_tokens if self.prefix_tokens else 0.0

    @property
    def all_passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def _role_token_share(messages: list[Message]) -> dict[str, float]:
    """按角色统计 token 占比——用来确认数据集是否贴近真实会话。"""
    buckets = {"user": 0, "assistant": 0, "tool_result": 0}
    for m in messages:
        tok = estimate_tokens([m])
        if m.tool_results:
            buckets["tool_result"] += tok
        else:
            buckets[m.role] = buckets.get(m.role, 0) + tok
    total = sum(buckets.values()) or 1
    return {k: v / total for k, v in buckets.items()}


def _has_isolated_tool_result(messages: list[Message]) -> str:
    """keep 窗口里不允许出现"配不上对的 tool_result"。

    一个孤立的 tool_result 意味着模型拿到一个无法归属到任何 tool_use 的结果，
    会直接导致 API 报错。
    """
    open_ids: set[str] = set()
    for m in messages:
        for tu in m.tool_uses:
            open_ids.add(tu.tool_use_id)
        if m.tool_results:
            for tr in m.tool_results:
                if tr.tool_use_id not in open_ids:
                    return tr.tool_use_id
        # 只有 assistant 才会"发起"调用；user 的 tool_results 消费它们
        if m.role == "assistant" and not m.tool_uses:
            open_ids.clear()
    return ""


async def audit_case(
    case: ProbeCase,
    client: Any,
    session_dir: Path,
) -> MechanismReport:
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    before_dist = _role_token_share(case.messages)
    conv = ConversationManager(history=list(case.messages))
    original = list(case.messages)
    before = conv.current_tokens()

    # 先算一遍切分，这样 auto_compact 不触发时能给出具体原因，而不是含糊的"未触发"
    from mewcode.context.manager import (
        _compute_keep_start_index,
        _prefix_too_small_to_compact,
    )

    probe_keep_start = _compute_keep_start_index(conv.history)
    probe_prefix_tokens = estimate_tokens(conv.history[:probe_keep_start])

    event = await auto_compact(
        conv,
        client,
        200_000,
        session_dir,
        protocol="anthropic",
        manual=True,
        transcript_path="",
    )

    rep = MechanismReport(
        case_id=case.case_id,
        before_tokens=before,
        total_messages=len(original),
        role_share=before_dist,
    )

    def check(name: str, ok: bool, detail: str = "") -> None:
        rep.checks.append((name, ok, detail))

    # M1 压缩真的发生了
    if not isinstance(event, CompactEvent) or event.boundary is None:
        if event is None:
            detail = (
                f"返回 None：keep_start={probe_keep_start}（须 >0）、"
                f"前缀 {probe_prefix_tokens} tokens（须 ≥ {MIN_SUMMARIZE_PREFIX_TOKENS}）"
            )
        else:
            detail = f"返回非 CompactEvent：{type(event).__name__} = {str(event)[:300]}"
        check("M1 压缩触发", False, detail)
        rep.after_tokens = conv.current_tokens()
        return rep
    check("M1 压缩触发", True)

    summary = event.boundary.summary
    keep = list(event.boundary.keep)
    new_history = list(conv.history)
    keep_start = len(original) - len(keep)

    rep.keep_start = keep_start
    rep.keep_count = len(keep)
    rep.after_tokens = conv.current_tokens()

    # M2 机制必须真的省 token
    check(
        "M2 净省 token",
        rep.after_tokens < before,
        f"{before} → {rep.after_tokens}（比 {rep.compression_ratio:.3f}）",
    )

    # M3 keep 尾部必须与原文逐条**同一对象**（原样保留，不能被改写）
    mismatched = [
        i for i, m in enumerate(keep) if original[keep_start + i] is not m
    ]
    check(
        "M3 keep 原样保留",
        not mismatched,
        f"keep={len(keep)} 条，位置 {mismatched[:3]} 不是同一对象" if mismatched else "",
    )

    # M4 新 history 必须恰好 = [摘要消息] + keep
    expected_prefix = build_compact_messages(
        summary, attachment="", has_keep_tail=bool(keep), transcript_path=""
    )
    check(
        "M4 history 结构",
        len(new_history) == len(expected_prefix) + len(keep),
        f"实际 {len(new_history)} 条，期望 {len(expected_prefix) + len(keep)} 条",
    )
    check(
        "M4 摘要消息为首",
        bool(new_history) and new_history[0].role == "user"
        and summary.split("\n")[0] in new_history[0].content,
    )

    # M5 keep 尾部不许出现孤立 tool_result
    orphan = _has_isolated_tool_result(keep)
    check("M5 配对完整", not orphan, f"孤立 tool_result: {orphan}" if orphan else "")

    # M6 摘要非空，且 <analysis> 已被剥离
    check("M6 摘要非空", bool(summary.strip()), f"{len(summary)} 字符")
    analysis_leaked = "<analysis>" in summary or "</analysis>" in summary
    check(
        "M6 analysis 已剥离",
        not analysis_leaked,
        "摘要里仍残留 <analysis> 标签" if analysis_leaked else "",
    )
    # 直接验证 extract_summary 的兜底行为（已知缺陷，见 test_extract_summary.py）
    leaked = extract_summary("<analysis>草稿</analysis>")
    check(
        "M6b 兜底不泄漏（已知缺陷）",
        "草稿" not in leaked,
        "模型漏输出 <summary> 时 <analysis> 会被整段塞进上下文",
    )

    # M7 被摘要的前缀必须真的不在新 history 里（按身份判断）
    new_ids = {id(m) for m in new_history}
    leftover = [i for i, m in enumerate(original[:keep_start]) if id(m) in new_ids]
    check(
        "M7 前缀已丢弃",
        not leftover,
        f"前缀中仍有 {len(leftover)} 条留在 history 里",
    )

    # M8 token 账目：before ≈ prefix + keep，after ≈ 摘要消息 + keep
    rep.prefix_tokens = estimate_tokens(original[:keep_start])
    rep.keep_tokens = estimate_tokens(keep)
    rep.summary_tokens = estimate_tokens([Message(role="user", content=summary)])
    rep.attachment_tokens = max(
        0, rep.after_tokens - rep.summary_tokens - rep.keep_tokens
    )
    accounted = rep.prefix_tokens + rep.keep_tokens
    check(
        "M8 账目对得上",
        abs(accounted - before) <= max(50, before * 0.05),
        f"before={before} vs prefix({rep.prefix_tokens})+keep({rep.keep_tokens})={accounted}",
    )

    # M9 keep 窗口必须遵守常量约束
    within_tokens = rep.keep_tokens <= KEEP_MAX_TOKENS
    within_count = len(keep) >= MIN_KEEP_MESSAGES or rep.keep_tokens >= KEEP_RECENT_TOKENS
    check(
        "M9 keep 窗口合规",
        within_tokens and within_count,
        f"keep={len(keep)} 条 / {rep.keep_tokens} tokens（KEEP_MAX={KEEP_MAX_TOKENS}）",
    )

    return rep


def format_audit(reports: list[MechanismReport], title: str) -> str:
    lines = [f"\n=== {title} ==="]
    all_ok = True
    for r in reports:
        lines.append(f"\n【{r.case_id}】原文 {r.total_messages} 条消息 / {r.before_tokens} tokens")
        shares = "  ".join(f"{k}={v:.0%}" for k, v in r.role_share.items())
        lines.append(f"  角色占比: {shares}")
        lines.append(
            f"  切分: prefix={r.prefix_tokens} tokens（{r.keep_start} 条）"
            f" + keep={r.keep_tokens} tokens（{r.keep_count} 条）"
        )
        lines.append(
            f"  压缩: {r.before_tokens} → {r.after_tokens} tokens"
            f"  压缩比 {r.compression_ratio:.3f}"
        )
        lines.append(
            f"  组成: 摘要={r.summary_tokens} tokens"
            f"（占前缀 {r.summary_vs_prefix:.0%}）"
            f" + keep={r.keep_tokens} + 附件/包装={r.attachment_tokens}"
        )
        for name, ok, detail in r.checks:
            mark = "✅" if ok else "❌"
            suffix = f"  {detail}" if detail else ""
            lines.append(f"    {mark} {name}{suffix}")
            if not ok:
                all_ok = False
    lines.append("\n" + ("✅ 机制不变量全部通过" if all_ok else "❌ 存在机制问题，见上面标记"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.retention.audit")
    p.add_argument("--real", action="store_true", help="用真实 LLM 做摘要（花钱）")
    p.add_argument("--strategy", choices=("naive", "structured"), default="structured")
    p.add_argument(
        "--distorted",
        action="store_true",
        help="用 99%%-user 的失真数据集（对比用），默认用真实角色分布",
    )
    p.add_argument("--sizes", default="16", help="长度档，单位 k tokens")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sizes = tuple(int(x.strip()) * 1000 for x in args.sizes.split(",") if x.strip())

    cases = [
        build_case(
            f"audit{size // 1000}k",
            target_tokens=size,
            probes_per_type=1,
            realistic=not args.distorted,
        )
        for size in sizes
    ]

    session_dir = Path(".eval-tmp/audit/session")
    session_dir.mkdir(parents=True, exist_ok=True)

    if args.real:
        from mewcode.client import create_client
        from mewcode.config import load_config

        from tests.retention.summarizers import RealSummarizer

        cfg = load_config()
        client = RealSummarizer(create_client(cfg.providers[0])).make_client()
        title = f"真实 LLM 压缩机制体检（{cfg.providers[0].model}）"
    else:
        from tests.retention.summarizers import (
            FakeSummarizer,
            naive_summary,
            structured_summary,
        )

        strategy = naive_summary if args.strategy == "naive" else structured_summary
        client = FakeSummarizer(strategy, args.strategy).make_client()
        title = f"机制体检（伪造摘要器={args.strategy}）—— 压缩比不代表真实值"

    reports = asyncio.run(
        _run_all(cases, client, session_dir)
    )
    print(format_audit(reports, title))
    return 0


async def _run_all(
    cases: list[ProbeCase], client: Any, session_dir: Path
) -> list[MechanismReport]:
    out: list[MechanismReport] = []
    for c in cases:
        out.append(await audit_case(c, client, session_dir))
    return out


if __name__ == "__main__":
    sys.exit(main())

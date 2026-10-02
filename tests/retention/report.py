"""命令行入口：跑一遍上下文信息保留评测并打印报告。

    # 零成本：伪造摘要器，验证流程与指标（不是真实保留率）
    python -m tests.retention.report

    # 对比「旧模板 vs 新模板」两种摘要策略
    python -m tests.retention.report --strategy naive
    python -m tests.retention.report --strategy structured

    # 真实 LLM（花钱）
    python -m tests.retention.report --real --probes-per-type 1 --trials 1

    # 把数据集落盘，便于人工审阅/版本化
    python -m tests.retention.report --dump .eval-tmp/dataset.json
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from tests.retention.generator import build_dataset, write_dataset
from tests.retention.harness import format_report, run_dataset


def _parse_sizes(raw: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) * 1000 for x in raw.split(",") if x.strip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tests.retention.report",
        description="上下文信息保留评测",
    )
    p.add_argument(
        "--real",
        action="store_true",
        help="用真实 LLM 摘要与答题（花钱）。默认用伪造摘要器，只验证流程。",
    )
    p.add_argument(
        "--strategy",
        choices=("naive", "structured"),
        default="structured",
        help="伪造摘要策略：naive=旧模板(概括型)，structured=新模板(用户原话保留)",
    )
    p.add_argument("--sizes", default="8,16,32", help="长度扫描档位，单位 k tokens")
    p.add_argument("--probes-per-type", type=int, default=1, help="每类探针数量")
    p.add_argument("--trials", type=int, default=1, help="每探针重复采样次数（真实模式下才有效）")
    p.add_argument("--no-recovery", action="store_true", help="关闭恢复附件（隔离恢复通道）")
    p.add_argument(
        "--distorted",
        action="store_true",
        help="用 99%%-user 的失真数据集做对照；默认用贴近真实会话的角色分布",
    )
    p.add_argument("--dump", default="", help="把生成的数据集写到指定 JSON 路径")
    p.add_argument("--workdir", default=".eval-tmp/run", help="session 目录")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cases = build_dataset(
        sizes=_parse_sizes(args.sizes),
        probes_per_type=args.probes_per_type,
        realistic=not args.distorted,
    )
    total_probes = sum(len(c.probes) for c in cases)
    print(
        f"数据集：{len(cases)} 条用例 / {total_probes} 个探针 "
        f"（长度档 {args.sizes}k，每类 {args.probes_per_type} 个，"
        f"角色分布={'失真 99%-user' if args.distorted else '真实'}）"
    )

    if args.dump:
        path = write_dataset(cases, args.dump)
        print(f"已写出数据集：{path}")

    session_dir = Path(args.workdir) / "session"
    session_dir.mkdir(parents=True, exist_ok=True)

    if args.real:
        from mewcode.client import create_client
        from mewcode.config import load_config

        from tests.retention.answerers import LLMAnswerer
        from tests.retention.summarizers import RealSummarizer

        cfg = load_config()
        client = create_client(cfg.providers[0])
        summarizer = RealSummarizer(client)
        answerer = LLMAnswerer(client)
        title = f"真实 LLM（provider={cfg.providers[0].name}, model={cfg.providers[0].model}）"
    else:
        from tests.retention.answerers import LexicalAnswerer
        from tests.retention.summarizers import (
            FakeSummarizer,
            naive_summary,
            structured_summary,
        )

        strategy = naive_summary if args.strategy == "naive" else structured_summary
        summarizer = FakeSummarizer(strategy, args.strategy)
        answerer = LexicalAnswerer()
        title = (
            f"流程验证（伪造摘要器={args.strategy} + 字面答题器）"
            "—— 数字仅证明流水线有效，不代表真实保留率"
        )

    runs = asyncio.run(
        run_dataset(
            cases,
            summarizer,
            answerer,
            session_dir,
            with_recovery=not args.no_recovery,
        )
    )
    print(format_report(runs, title=title))

    invalid = [r for r in runs if not r.valid]
    if invalid:
        print(
            f"\n提示：{len(invalid)}/{len(runs)} 条用例因为没触发压缩被判无效。"
            "长度档调大（--sizes）通常能解决。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

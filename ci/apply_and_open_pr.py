#!/usr/bin/env python3
"""CI 侧：接收 agent 产出的补丁 → 独立重跑验证 → 推送 → 开 PR。

**这个脚本里没有一行 AI。** 它是整条流水线的"确定性外壳"：
agent 已经在容器里退场了，剩下的事必须机械、可重试、可审计。

    python ci/apply_and_open_pr.py \
        --repo . --artifacts /artifacts \
        --verify-cmd "python -m pytest tests/ -q" \
        --base main

## 三个必须做对的地方

① **独立重跑验证**
   agent 自己跑过一次（`CreatePR` 里的命令验证），但**不能信**：
   它可能跑了错误的命令、跳过了部分测试、或者在改完测试后没重跑。
   这一步是真正的裁判。

② **绝不推到基线分支**
   分支名由标题派生 + 加后缀，并且显式检查不等于 base。
   推错分支是最难挽回的一类事故。

③ **失败要留痕，不要静默退出**
   验证失败时写 `FAILURE.md` 并把 agent 自己的验证输出一起附上，
   这样人能看出"是 agent 谎报通过"还是"代码真的坏了"。

## 模式

    --dry-run     只做 apply + 重跑验证，不推送、不开 PR（CI 里最常用的调试档）
    --no-pr       推送分支，但不开 PR
    （默认）      全流程
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ci/ 不是包，直接跑脚本时仓库根不在 sys.path 上。
# （editable 安装或容器里的 PYTHONPATH=/app 已经覆盖了，这里只是兜底。）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mewcode.delivery import (  # noqa: E402
    DeliveryError,
    derive_branch,
    git,
    guard_branch,
    head_sha,
    open_pr,
    push_branch,
    run,
    split_command,
)

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def fail(artifacts: Path, stage: str, detail: str, extra: str = "") -> int:
    """写失败报告并返回非 0。绝不静默退出。"""
    body = [
        "# ❌ Agent 流水线失败",
        "",
        f"**阶段**：{stage}",
        "",
        "## 原因",
        "",
        "```",
        detail.strip()[:4000],
        "```",
    ]
    if extra:
        body += ["", "## 附：agent 自己的验证结果（供对照）", "", "```json", extra, "```"]
    (artifacts / "FAILURE.md").write_text("\n".join(body), encoding="utf-8")
    print(f"\n❌ 失败于「{stage}」\n{detail}", file=sys.stderr)
    print(f"\n报告已写入 {artifacts / 'FAILURE.md'}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="应用 agent 补丁并开 PR（无 AI）")
    ap.add_argument("--repo", default=".", help="仓库目录")
    ap.add_argument("--artifacts", default="/artifacts", help="agent 产物目录")
    ap.add_argument("--verify-cmd", default="python -m pytest tests/ -q")
    ap.add_argument("--base", default="main", help="基线分支")
    ap.add_argument("--remote", default="origin")
    ap.add_argument(
        "--api-base",
        default="",
        help="PR API 端点，默认 https://api.github.com；"
             "自建 GitHub Enterprise 填 https://<host>/api/v3 "
             "（也可用环境变量 GITHUB_API_URL）",
    )
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true", help="只 apply + 重跑验证")
    ap.add_argument("--no-pr", action="store_true", help="推送但不建 PR")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    artifacts = Path(args.artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    # ── 1. 前置检查 ───────────────────────────────────────────
    patch = artifacts / "changes.patch"
    if not patch.exists():
        return fail(artifacts, "读取补丁", f"{patch} 不存在：agent 没有产出任何改动")
    if not patch.read_text(encoding="utf-8", errors="replace").strip():
        return fail(artifacts, "读取补丁", "补丁为空")

    meta: dict = {}
    meta_path = artifacts / "pr.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return fail(artifacts, "读取元信息", f"pr.json 解析失败：{e}")
    title = meta.get("title") or "agent: 自动改动"
    agent_verify = json.dumps(
        meta.get("verify") or {}, ensure_ascii=False, indent=2
    ) + (
        "\n"
        + json.dumps(meta.get("independent_verify") or {}, ensure_ascii=False, indent=2)
    )
    # 验证者的**完整推理** —— 失败时这是最有用的东西：人需要看出
    # "是 agent 谎报了通过"还是"验证者判错了"。
    verification_md = artifacts / "verification.md"
    if verification_md.exists():
        agent_verify += "\n\n--- 验证者原话 ---\n" + verification_md.read_text(
            encoding="utf-8", errors="replace"
        )[:4000]

    print(f"→ 仓库      : {repo}")
    print(f"→ 产物      : {artifacts}")
    print(f"→ 标题      : {title}")
    print(f"→ 基线分支  : {args.base}")

    code, out = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if code != 0:
        return fail(artifacts, "检查仓库", f"{repo} 不是 git 仓库")

    # ── 2. 应用补丁（先 --check，不要apply一半失败）────────────
    code, out = git(repo, "apply", "--check", str(patch), check=False)
    if code != 0:
        return fail(artifacts, "应用补丁", f"git apply --check 失败：\n{out}", agent_verify)
    git(repo, "apply", str(patch))
    print("✅ 补丁已应用")

    # ── 3. 独立重跑验证 ★ 这一步是真正的裁判 ──────────────────
    print(f"\n→ 独立重跑：{args.verify_cmd}")
    rc, vout = run(split_command(args.verify_cmd), repo, timeout=args.timeout)
    if rc != 0:
        return fail(
            artifacts,
            "独立验证",
            f"命令：{args.verify_cmd}\n退出码：{rc}\n\n{vout[-4000:]}",
            agent_verify,
        )
    print(f"✅ 独立验证通过（退出码 0）")

    if args.dry_run:
        print("\n--dry-run：到此为止，不推送、不开 PR")
        return 0

    # ── 4. 推送分支（绝不推基线）─────────────────────────────
    branch = derive_branch(title, head_sha(repo))
    try:
        guard_branch(branch, args.base, args.remote)
    except DeliveryError as e:
        return fail(artifacts, "建分支", str(e))

    # 用 -B 而不是 checkout -b：重试时不会因为分支已存在而失败
    git(repo, "checkout", "-B", branch)
    git(repo, "add", "-A")
    rc, out = run(["git", "diff", "--cached", "--quiet"], repo)
    if rc == 0:
        return fail(artifacts, "提交", "补丁应用后没有产生任何改动")
    git(repo, "commit", "-m", title)
    print(f"✅ 已提交到 {branch}")

    ok, out = push_branch(repo, branch, args.base, args.remote, args.timeout)
    if not ok:
        return fail(artifacts, "推送", out, agent_verify)
    print(f"✅ {out}")

    # ── 5. 开 PR ─────────────────────────────────────────────
    if args.no_pr:
        print("\n--no-pr：跳过开 PR")
        print(f"\n分支已就绪：{branch}")
        return 0

    body = _build_pr_body(meta, agent_verify)
    body_file = artifacts / "pr.md"
    body_file.write_text(body, encoding="utf-8")

    ok, msg = open_pr(
        repo, title, body_file, args.base, branch,
        api_base=args.api_base, remote=args.remote,
    )
    if not ok:
        # 推送成功了但 PR 没开成 —— 这不算彻底失败，人要能接手
        print(f"\n⚠️ 分支已推送，但自动开 PR 失败：{msg}", file=sys.stderr)
        print(f"请手动开 PR：{branch} → {args.base}", file=sys.stderr)
        print(f"PR 描述已写好：{body_file}", file=sys.stderr)
        return 0
    print(f"✅ {msg}")
    return 0


def _build_pr_body(meta: dict, agent_verify: str) -> str:
    ind = meta.get("independent_verify") or {}
    return "\n".join(
        [
            "## 由 agent 生成",
            "",
            meta.get("description") or "(无描述)",
            "",
            f"关联：{meta.get('issue') or '(未关联)'}",
            "",
            "## 改动",
            "",
            "```",
            meta.get("diff_stat") or "(无)",
            "```",
            "",
            "## 验证",
            "",
            "| 阶段 | 结果 |",
            "|---|---|",
            f"| agent 自跑的命令验证 | 退出码 {meta.get('verify', {}).get('exit_code', '?')} |",
            f"| agent 独立验证 | {ind.get('verdict', '未启用')} |",
            f"| **CI 独立重跑** | ✅ 通过（退出码 0） |",
            "",
            "<details><summary>agent 的验证记录</summary>",
            "",
            "```json",
            agent_verify,
            "```",
            "",
            "</details>",
            "",
            "---",
            "",
            "⚠️ 请人工 review。CI 通过只代表测试通过，不代表改动正确。",
        ]
    )



if __name__ == "__main__":
    sys.exit(main())

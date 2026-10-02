"""CreatePR：验证通过才产出补丁，**但绝不推送**。

## 为什么这样设计

这是"云端代码运维 agent"里最关键的一道边界。让 AI 直接 `git push` 有三个问题：

  1. **凭证问题**：要 push 就得给它长期 token，而有 token 就能删分支、改仓库设置、
     把代码推到别处。安全边界等于没有。
  2. **验证问题**：靠提示词要求"验证通过才提 PR"，模型可以不遵守。
  3. **不可逆**：push 出去的东西，撤回要打补丁、要通知。

所以这里的设计是：

    AI 的职责：改代码 → 产出 patch          ← 本工具
    脚本的职责：接收 patch → push → 开 PR   ← CI 的确定性步骤

**AI 根本没有推送权限，"乱推代码"在物理上不可能发生。**

## 两种情况都会拒绝

  - 验证命令退出码非 0（测试没过 / 类型检查失败）
  - 与基线分支相比没有任何改动（空补丁）

拒绝时返回 `is_error=True` 并把**验证输出**带回去，让模型自己知道哪里没过、去改，
而不是靠人转述。
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from mewcode.tools.agent_verify import Verdict, VerifierRunner, parse_verdict
from mewcode.tools.base import Tool, ToolResult

#: 验证输出回传给模型时的截断长度（保留尾部，报错通常在尾部）
_TAIL_CHARS = 2_000


@dataclass
class VerifyResult:
    ok: bool
    command: str
    exit_code: int
    output: str
    elapsed: float


def _run(
    argv: list[str], cwd: str, timeout: int
) -> tuple[int, str]:
    """跑一个命令，返回 (退出码, 合并后的输出)。

    用列表形式传参（不走 shell），避免命令注入；编码固定 utf-8，
    避免 Windows 下中文输出乱码。
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"命令超时（{timeout}s）：{' '.join(argv)}"
    except FileNotFoundError as e:
        return 127, f"命令不存在：{e}"
    except OSError as e:
        return 126, f"命令无法执行：{e}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _split_command(command: str) -> list[str]:
    """把验证命令切成 argv。

    ⚠️ 不能直接用 `shlex.split(command, posix=False)`：在 Windows 上它**保留引号**，
    于是 `"C:\\Program Files\\py.exe" -m pytest` 的第一个 token 会带着引号，
    `subprocess` 按字面去找这个文件名 → FileNotFoundError（退出码 127）。

    这里手动剥掉每个 token 两端**成对**的引号。命令本身来自运维配置（可信），
    不是模型输入，所以不需要担心注入；用 argv 而不是 shell 只是为了少一层不确定性。
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return [command]
    out: list[str] = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        out.append(t)
    return out or [command]


def make_verifier(
    verify_command: str, work_dir: str, timeout: int
) -> Callable[[], VerifyResult]:
    """默认验证器：执行一条验证命令，用**退出码**判定成败。

    退出码是这个门禁唯一信任的信号——模型无法伪造它。
    """

    def _verify() -> VerifyResult:
        t0 = time.monotonic()
        argv = _split_command(verify_command)
        code, out = _run(argv, work_dir, timeout)
        return VerifyResult(
            ok=(code == 0),
            command=verify_command,
            exit_code=code,
            output=out,
            elapsed=time.monotonic() - t0,
        )

    return _verify


class CreatePRParams(BaseModel):
    title: str = Field(description="PR 标题，一行，遵循仓库的提交规范")
    description: str = Field(default="", description="PR 描述：改了什么 / 为什么 / 怎么验证的")
    issue: str = Field(default="", description="关联的 issue 编号，例如 #142")


class CreatePRTool(Tool):
    name = "CreatePR"
    description = (
        "提交 PR 申请。执行前会**自动跑一遍验证命令**（测试 / 类型检查），"
        "验证不通过会被拒绝并返回失败输出。\n"
        "验证包含两层：先跑命令看退出码，再由一个独立验证者审你的改动；"
        "两层都过了才会交付。\n"
        "交付方式由配置决定：`patch` 模式只产出补丁文件（由 CI 收尾推送）；"
        "`push` / `pr` 模式下它会自己推送 `agent/*` 分支，`pr` 模式还会直接开 PR。\n"
        "**它永远不会推基线分支，也永远不会合并。**"
    )
    params_model = CreatePRParams
    # command 类：有外部副作用（可能推送分支 / 开 PR），不做并发安全优化
    category = "command"
    is_concurrency_safe = False

    #: 交付模式：patch（只出补丁）/ push（推 agent/* 分支）/ pr（再开 PR）
    MODES = ("patch", "push", "pr")

    def __init__(
        self,
        work_dir: str,
        verify_command: str = "pytest -q",
        artifacts_dir: str | None = None,
        base_ref: str = "main",
        timeout: int = 600,
        verifier: Callable[[], VerifyResult] | None = None,
        verifier_runner: VerifierRunner | None = None,
        require_independent: bool = False,
        mode: str = "patch",
        remote: str = "origin",
        api_base: str = "",
    ) -> None:
        self._work_dir = str(Path(work_dir).resolve())
        self._verify_command = verify_command
        self._artifacts = Path(artifacts_dir) if artifacts_dir else (
            Path(self._work_dir) / ".mewcode" / "pr"
        )
        self._base_ref = base_ref
        self._timeout = timeout
        self._verify = verifier or make_verifier(verify_command, self._work_dir, timeout)
        #: 独立验证者（子 agent）。为 None 时跳过独立验证这一层。
        self._verifier_runner = verifier_runner
        #: 为 True 且没有 verifier_runner 时直接拒绝 —— 生产环境应该打开，
        #: 避免"忘了接独立验证"却以为已经接上了。
        self._require_independent = require_independent
        if mode not in self.MODES:
            raise ValueError(f"mode 必须是 {self.MODES} 之一，收到 {mode!r}")
        #: 交付模式。`patch` = 只出补丁（容器模式，由 CI 推送）；
        #: `push` = 自己推 agent/* 分支；`pr` = 再开 PR。
        self._mode = mode
        self._remote = remote
        self._api_base = api_base
        #: 最近一次交付的结果，供测试与上层读取
        self.last_delivery: dict[str, str] = {}

    # -- git 辅助 --------------------------------------------------------

    def _git(self, *args: str) -> tuple[int, str]:
        return _run(["git", *args], self._work_dir, self._timeout)

    def _ensure_repo(self) -> str | None:
        code, out = self._git("rev-parse", "--is-inside-work-tree")
        if code != 0 or out.strip() != "true":
            return "当前目录不是 git 仓库，无法生成补丁"
        return None

    def _diff_stat(self) -> str:
        code, out = self._git("diff", "--stat", self._base_ref)
        return out.strip() if code == 0 else ""

    async def _run_independent(self, p: CreatePRParams) -> Verdict | None:
        """跑独立验证者。异常不吞——让上层看到真实的失败原因。

        验证者的输出**整段**保留（截断到 8000 字符），因为它还要进
        `verification.json` 供 CI 与人工审阅。
        """
        desc = p.description or p.title
        if p.issue:
            desc = f"{desc}\n关联 issue：{p.issue}"
        diff_stat = self._diff_stat()
        if diff_stat:
            desc = f"{desc}\n\n改动摘要：\n{diff_stat}"
        raw = await self._verifier_runner(desc)  # type: ignore[misc]
        return parse_verdict(raw)

    # -- 主流程 ----------------------------------------------------------

    async def execute(self, params: BaseModel) -> ToolResult:
        p: CreatePRParams = params  # type: ignore[assignment]

        err = self._ensure_repo()
        if err:
            return ToolResult(output=f"提 PR 失败：{err}", is_error=True)

        # ① 命令验证 —— 最便宜的一层，先跑，快速失败
        result = self._verify()
        if not result.ok:
            tail = result.output[-_TAIL_CHARS:]
            return ToolResult(
                output=(
                    f"❌ 提 PR 被拒：验证未通过\n\n"
                    f"验证命令：{result.command}\n"
                    f"退出码：{result.exit_code}\n"
                    f"耗时：{result.elapsed:.1f}s\n\n"
                    f"输出（尾部 {_TAIL_CHARS} 字符）：\n{tail}\n\n"
                    f"请先修复上述问题，再重新调用 CreatePR。"
                ),
                is_error=True,
            )

        # ② 独立验证 —— 贵的那一层，由**另一个 agent**做，不是实现者自己说过了
        verdict: Verdict | None = None
        if self._verifier_runner is not None:
            verdict = await self._run_independent(p)
            if verdict is not None and not verdict.passed:
                return ToolResult(
                    output=(
                        f"❌ 提 PR 被拒：**独立验证**未通过\n\n"
                        f"（命令验证已通过：{result.command} → 退出码 0，"
                        f"{result.elapsed:.1f}s）\n\n"
                        f"独立验证者的结论：\n{verdict.reason}\n\n"
                        f"注意：这不是「测试没过」，是另一个 agent 在审你的改动时"
                        f"找到了问题。请针对上面的分析修复，而不是重跑一遍测试。"
                    ),
                    is_error=True,
                )
        elif self._require_independent:
            return ToolResult(
                output=(
                    "❌ 提 PR 被拒：本环境要求独立验证，但没有配置验证者。\n"
                    "请用 `make_subagent_verifier(parent_agent)` 造一个并传给 "
                    "`CreatePRTool(verifier_runner=...)`。"
                ),
                is_error=True,
            )

        # ② 有改动吗 —— 空补丁没有意义
        patch_code, patch = self._git("diff", self._base_ref)
        if patch_code != 0:
            return ToolResult(
                output=(
                    f"提 PR 失败：无法与基线 {self._base_ref} 比较差异\n"
                    f"（基线分支可能不存在，用 base_ref 参数指定）"
                ),
                is_error=True,
            )
        if not patch.strip():
            return ToolResult(
                output=f"提 PR 失败：与 {self._base_ref} 相比没有任何改动，无需提交",
                is_error=True,
            )

        # ③ 产出补丁（写到 AI 够得着、但推送流程读得到的地方）
        self._artifacts.mkdir(parents=True, exist_ok=True)
        patch_path = self._artifacts / "changes.patch"
        patch_path.write_text(patch, encoding="utf-8")

        meta = {
            "title": p.title,
            "description": p.description,
            "issue": p.issue,
            "base_ref": self._base_ref,
            "diff_stat": self._diff_stat(),
            "verify": {
                "command": result.command,
                "exit_code": result.exit_code,
                "elapsed_s": round(result.elapsed, 2),
            },
            "independent_verify": (
                {
                    "verdict": "PASS" if verdict.passed else "FAIL",
                    "reason": verdict.reason,
                }
                if verdict is not None
                else {"verdict": "SKIPPED", "reason": "本环境未配置独立验证者"}
            ),
        }
        (self._artifacts / "pr.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 独立验证的完整输出单独落盘，供 CI 与人工审阅
        if verdict is not None:
            (self._artifacts / "verification.md").write_text(
                verdict.raw, encoding="utf-8"
            )

        stages = "命令验证（退出码 0）"
        if verdict is not None:
            stages += " + 独立验证（VERDICT: PASS）"
        elif self._require_independent:
            stages += " + 独立验证（已通过）"
        else:
            stages += "；⚠️ **独立验证未启用**（未配置 verifier_runner）"

        # ④ 交付 —— **只可能发生在两层验证都通过之后**
        #
        # 顺序是这个工具的核心：验证在前，推送在后。任何"先推再说"的实现
        # 都会让门禁失去意义（推上去的分支即使 PR 没开，也已经到远端了）。
        if self._mode == "patch":
            return ToolResult(
                output=(
                    f"✅ {stages}\n\n"
                    f"命令验证：{result.command} → 退出码 0（{result.elapsed:.1f}s）\n"
                    f"补丁已生成：{patch_path}\n"
                    f"PR 元信息：{self._artifacts / 'pr.json'}\n"
                    f"改动摘要：\n{meta['diff_stat']}\n\n"
                    f"标题：{p.title}\n"
                    f"关联：{p.issue or '(未关联)'}\n\n"
                    f"交付模式：patch —— 本工具**未推送**任何代码。"
                    f"CI 会对该补丁重新跑一遍验证，通过后才推送并创建 PR。"
                )
            )

        return await self._deliver(p, meta, stages, patch_path)

    async def _deliver(
        self, p: CreatePRParams, meta: dict, stages: str, patch_path: Path
    ) -> ToolResult:
        """提交 → 推 `agent/*` 分支 → （pr 模式下）开 PR。

        **绝不推基线分支**由 `delivery.guard_branch` 结构性保证：分支名不以
        `agent/` 开头就直接拒绝，所以这条路推不到 `main` —— 不是"记得别推"，
        而是走不通。
        """
        from mewcode import delivery

        try:
            branch = delivery.derive_branch(p.title, delivery.head_sha(self._work_dir))
            delivery.checkout_branch(self._work_dir, branch)
        except delivery.DeliveryError as e:
            return ToolResult(output=f"❌ 交付失败（建分支）：{e}", is_error=True)

        committed, why = delivery.commit_all(self._work_dir, p.title)
        if not committed:
            return ToolResult(
                output=(
                    f"❌ 交付失败（提交）：{why}\n\n"
                    f"两层验证都已通过（{stages}），但工作区没有可提交的改动。\n"
                    f"补丁文件仍然写出来了：{patch_path}"
                ),
                is_error=True,
            )

        ok, msg = delivery.push_branch(
            self._work_dir, branch, self._base_ref, self._remote, self._timeout
        )
        if not ok:
            return ToolResult(
                output=(
                    f"❌ 交付失败（推送）：\n{msg}\n\n"
                    f"两层验证都已通过（{stages}），本地分支 `{branch}` 已建好，"
                    f"但推不上去。常见原因：没有远端、没有推送凭据、或网络不通。\n"
                    f"补丁文件仍可用：{patch_path}\n"
                    f"（推送失败**不会**影响基线分支，它一个 commit 都没动。）"
                ),
                is_error=True,
            )

        self.last_delivery = {"branch": branch, "push": msg}

        if self._mode == "push":
            return ToolResult(
                output=(
                    f"✅ {stages}\n\n"
                    f"{msg}\n"
                    f"补丁已生成：{patch_path}\n"
                    f"改动摘要：\n{meta['diff_stat']}\n\n"
                    f"标题：{p.title}\n"
                    f"分支：{branch} → 基线 {self._base_ref}\n\n"
                    f"交付模式：push —— 分支已推送，但**没有**开 PR"
                    f"（配置里 mode 不是 pr）。"
                )
            )

        # mode == "pr"：开 PR
        body_file = self._artifacts / "pr.md"
        body_file.write_text(self._build_body(p, meta, stages), encoding="utf-8")
        pr_ok, pr_msg = delivery.open_pr(
            self._work_dir, p.title, body_file, self._base_ref, branch,
            api_base=self._api_base, remote=self._remote,
        )
        if not pr_ok:
            # 分支已经推上去了，只是 PR 没开成 —— 这不算彻底失败，人要能接手
            return ToolResult(
                output=(
                    f"⚠️ {stages}，分支已推送，但自动开 PR 失败。\n\n"
                    f"{msg}\n"
                    f"开 PR 失败原因：{pr_msg}\n\n"
                    f"请手动开 PR：`{branch}` → `{self._base_ref}`\n"
                    f"PR 描述已写好：{body_file}\n"
                    f"（补丁：{patch_path}）"
                )
            )

        self.last_delivery["pr"] = pr_msg
        return ToolResult(
            output=(
                f"✅ {stages}\n\n"
                f"{msg}\n"
                f"{pr_msg}\n\n"
                f"改动摘要：\n{meta['diff_stat']}\n\n"
                f"标题：{p.title}\n"
                f"分支：{branch} → 基线 {self._base_ref}\n"
                f"补丁：{patch_path}\n\n"
                f"交付模式：pr —— 分支和 PR 都已创建。"
                f"**没有合并**，也没有碰 `{self._base_ref}`；合并由人点。"
            )
        )

    def _build_body(self, p: CreatePRParams, meta: dict, stages: str) -> str:
        ind = meta.get("independent_verify") or {}
        return "\n".join(
            [
                "## 由 agent 生成",
                "",
                p.description or "(无描述)",
                "",
                f"关联：{p.issue or '(未关联)'}",
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
                f"| 命令验证 | 退出码 {meta.get('verify', {}).get('exit_code', '?')} |",
                f"| 独立验证者 | {ind.get('verdict', '未启用')} |",
                "",
                f"验证链：{stages}",
                "",
                "---",
                "",
                "⚠️ 请人工 review。验证通过只代表测试通过，不代表改动正确。",
            ]
        )

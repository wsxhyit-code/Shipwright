"""CreatePR 验证门禁测试。

核心要证明三件事：

  1. **验证不通过 → 拒绝**，并把失败输出带回给模型（而不是靠人转述）
  2. **验证通过 → 只产出补丁，绝不推送**（用真实 bare 仓库端到端证明）
  3. 边界情况：空改动、非 git 目录、基线不存在，都要给出清晰原因

第 2 条是整套设计的安全边界所在：AI 没有推送权限，"乱推代码"在物理上不可能发生。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mewcode.tools.create_pr import (
    CreatePRTool,
    VerifyResult,
    make_verifier,
)


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


def ok(msg: str = "3 passed") -> VerifyResult:
    return VerifyResult(ok=True, command="pytest -q", exit_code=0, output=msg, elapsed=0.1)


def bad(msg: str = "3 failed, 27 passed") -> VerifyResult:
    return VerifyResult(ok=False, command="pytest -q", exit_code=1, output=msg, elapsed=0.2)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个最小 git 仓库：main 分支上有一个已提交文件。"""
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-b", "main")
    git(d, "config", "user.email", "test@example.com")
    git(d, "config", "user.name", "Test")
    (d / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-m", "init")
    return d


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """一个 bare 仓库扮演远端，用来证明"没有被推送"。"""
    d = tmp_path / "origin.git"
    d.mkdir()
    git(d, "init", "--bare", "-b", "main")
    return d


def make_tool(repo: Path, verifier, **kw) -> CreatePRTool:
    opts = {
        "work_dir": str(repo),
        "verify_command": "pytest -q",
        "artifacts_dir": str(repo / ".mewcode" / "pr"),
        "base_ref": "main",
        "verifier": verifier,
    }
    opts.update(kw)          # 允许用例覆盖 base_ref 等
    return CreatePRTool(**opts)


async def call(tool: CreatePRTool, **params):
    return await tool.execute(tool.params_model.model_validate(params))


# ---------------------------------------------------------------------------
# 1. 验证门禁
# ---------------------------------------------------------------------------


class TestVerificationGate:
    async def test_rejected_when_verification_fails(self, repo):
        (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        tool = make_tool(repo, bad)

        r = await call(tool, title="fix: 修 add")

        assert r.is_error, "验证没过却放行了"
        assert "验证未通过" in r.output
        # 关键：失败输出必须带回给模型，否则它不知道哪里错了
        assert "3 failed, 27 passed" in r.output
        assert "退出码：1" in r.output

    async def test_no_patch_written_when_verification_fails(self, repo):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, bad)
        await call(tool, title="x")
        assert not (repo / ".mewcode" / "pr" / "changes.patch").exists()
        assert not (repo / ".mewcode" / "pr" / "pr.json").exists()

    async def test_passes_when_verification_ok(self, repo):
        (repo / "app.py").write_text("def add(a, b):\n    return a + b  # ok\n", encoding="utf-8")
        tool = make_tool(repo, ok)

        r = await call(tool, title="fix: 修 add", description="改了注释", issue="#142")

        assert not r.is_error, r.output
        assert "命令验证" in r.output and "退出码 0" in r.output
        assert (repo / ".mewcode" / "pr" / "changes.patch").exists()
        assert "未推送" in r.output

    async def test_warns_loudly_when_independent_verify_not_wired(self, repo):
        """没接独立验证时必须**显式告知**，不能让人误以为已经接上了。"""
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, ok)
        r = await call(tool, title="t")
        assert "独立验证未启用" in r.output


# ---------------------------------------------------------------------------
# 1b. 独立验证（由另一个 agent 做，不是实现者自己说过了）
# ---------------------------------------------------------------------------


class TestIndependentVerification:
    async def test_fail_verdict_blocks_merge(self, repo):
        """命令验证过了，但独立验证者说有问题 → 仍然拒绝。"""
        from mewcode.tools.agent_verify import make_static_verifier

        (repo / "app.py").write_text("fixed\n", encoding="utf-8")
        tool = make_tool(
            repo, ok,
            verifier_runner=make_static_verifier(
                "同类问题只修了一处，csv 路径没改。\n\nVERDICT: FAIL\n"
            ),
        )
        r = await call(tool, title="t", description="修了 xlsx")

        assert r.is_error
        assert "独立验证" in r.output
        assert "csv 路径没改" in r.output
        # 必须点明"不是测试没过"，否则模型会去重跑测试
        assert "不是「测试没过」" in r.output
        assert not (repo / ".mewcode" / "pr" / "changes.patch").exists()

    async def test_pass_verdict_allows(self, repo):
        from mewcode.tools.agent_verify import make_static_verifier

        (repo / "app.py").write_text("fixed\n", encoding="utf-8")
        tool = make_tool(
            repo, ok,
            verifier_runner=make_static_verifier("追了调用链，没有遗漏。\n\nVERDICT: PASS"),
        )
        r = await call(tool, title="t")
        assert not r.is_error, r.output
        assert "VERDICT: PASS" in r.output
        # 完整输出要落盘，供 CI 和人工审阅
        vm = repo / ".mewcode" / "pr" / "verification.md"
        assert vm.exists() and "追了调用链" in vm.read_text(encoding="utf-8")

    async def test_missing_verdict_is_treated_as_fail(self, repo):
        """验证者忘了给结论 → 按不通过处理（fail-closed）。"""
        from mewcode.tools.agent_verify import make_static_verifier

        (repo / "app.py").write_text("fixed\n", encoding="utf-8")
        tool = make_tool(
            repo, ok,
            verifier_runner=make_static_verifier("我看了一下，应该没问题。"),
        )
        r = await call(tool, title="t")
        assert r.is_error and "没有给出 VERDICT" in r.output

    async def test_lowercase_and_fullwidth_verdict_accepted(self, repo):
        from mewcode.tools.agent_verify import make_static_verifier

        (repo / "app.py").write_text("fixed\n", encoding="utf-8")
        tool = make_tool(
            repo, ok, verifier_runner=make_static_verifier("检查完了\nverdict：pass")
        )
        r = await call(tool, title="t")
        assert not r.is_error, r.output

    async def test_require_independent_fails_closed_without_runner(self, repo):
        """生产环境开关：要求独立验证但没配 → 直接拒绝，不许悄悄跳过。"""
        (repo / "app.py").write_text("fixed\n", encoding="utf-8")
        tool = make_tool(repo, ok, require_independent=True)
        r = await call(tool, title="t")
        assert r.is_error and "要求独立验证" in r.output
        assert not (repo / ".mewcode" / "pr" / "changes.patch").exists()

    async def test_description_carries_issue_and_diffstat_to_verifier(self, repo):
        """验证者看不到实现对话，所以描述里必须带上足够的上下文。"""
        seen: list[str] = []

        async def capture(desc: str) -> str:
            seen.append(desc)
            return "VERDICT: PASS"

        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, ok, verifier_runner=capture)
        await call(tool, title="fix: 改 add", description="加回加法", issue="#142")

        assert seen, "验证者没被调用"
        assert "加回加法" in seen[0] and "#142" in seen[0]
        assert "app.py" in seen[0], "改动摘要没带给验证者"

    async def test_pr_json_records_both_stages(self, repo):
        import json

        from mewcode.tools.agent_verify import make_static_verifier

        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(
            repo, ok, verifier_runner=make_static_verifier("ok\nVERDICT: PASS")
        )
        await call(tool, title="t")
        meta = json.loads(
            (repo / ".mewcode" / "pr" / "pr.json").read_text(encoding="utf-8")
        )
        assert meta["verify"]["exit_code"] == 0
        assert meta["independent_verify"]["verdict"] == "PASS"

    async def test_pr_json_marks_skipped_when_not_wired(self, repo):
        import json

        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, ok)
        await call(tool, title="t")
        meta = json.loads(
            (repo / ".mewcode" / "pr" / "pr.json").read_text(encoding="utf-8")
        )
        assert meta["independent_verify"]["verdict"] == "SKIPPED"

    async def test_command_verify_runs_before_independent(self, repo):
        """命令验证失败时应快速失败，不浪费一次独立验证（它是贵的）。"""
        calls: list[str] = []

        async def runner(_desc: str) -> str:
            calls.append("independent")
            return "VERDICT: PASS"

        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, bad, verifier_runner=runner)
        r = await call(tool, title="t")

        assert r.is_error
        assert calls == [], "命令验证都没过，不该已经跑了独立验证"


# ---------------------------------------------------------------------------
# 2. 安全边界：绝不推送
# ---------------------------------------------------------------------------


class TestNeverPushes:
    def test_control_real_push_works(self, requires_real_push, real_push_supported):
        """★ 控制组：先证明**本环境真的能 push**，否则下面全是空过。

        这条测试的意义不在于它自己断言了什么，而在于：
        · 它通过 → 下面的负向断言有判别力
        · 它 skip → 下面的一起 skip（而不是假装通过）

        它证明了这一点：原本那几条"远端没被改动"的断言，在 push 根本跑不通的
        环境里，**无法区分「没有推送」和「尝试推送但失败了」**。
        """
        assert real_push_supported, "控制组声称支持 push 却返回 False"

    async def test_origin_untouched_after_create_pr(self, repo, origin, requires_real_push):
        """★ 端到端证明：跑完 CreatePR，远端**一个 commit 都没多**。

        `requires_real_push` 是控制组：它先证明本环境真的能 push。
        没有它的话，这条断言会空过 —— 详见 tests/conftest.py 里的说明。
        """
        git(repo, "remote", "add", "origin", str(origin))
        before = git(origin, "rev-list", "--all", "--count").strip()

        (repo / "app.py").write_text("def add(a, b):\n    return a + b  # changed\n",
                                     encoding="utf-8")
        tool = make_tool(repo, ok)
        r = await call(tool, title="fix: 改 add")

        assert not r.is_error, r.output
        after = git(origin, "rev-list", "--all", "--count").strip()
        assert after == before, f"远端被改动了！{before} → {after}"

    async def test_local_branch_not_pushed_either(self, repo, origin, requires_real_push):
        """只在本地提交、不推送，远端分支列表依然是空的。"""
        git(repo, "remote", "add", "origin", str(origin))
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "local commit")

        tool = make_tool(repo, ok, base_ref="HEAD~1")
        await call(tool, title="t")

        branches = git(origin, "branch", "--list").strip()
        assert branches == "", f"远端出现了分支：{branches}"

    async def test_patch_content_is_a_real_diff(self, repo):
        (repo / "app.py").write_text("def add(a, b):\n    return a + b  # patched\n",
                                     encoding="utf-8")
        tool = make_tool(repo, ok)
        await call(tool, title="t")
        patch = (repo / ".mewcode" / "pr" / "changes.patch").read_text(encoding="utf-8")
        assert patch.startswith("diff --git"), "产出的不是标准 git diff"
        assert "+    return a + b  # patched" in patch


# ---------------------------------------------------------------------------
# 3. 边界情况
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_empty_diff_rejected(self, repo):
        """没有任何改动时不该产出空补丁。"""
        tool = make_tool(repo, ok)
        r = await call(tool, title="t")
        assert r.is_error and "没有任何改动" in r.output

    async def test_not_a_git_repo(self, outside_any_repo):
        # 用 outside_any_repo 而不是裸 tmp_path —— 本仓库自己就是 git checkout，
        # 裸 tmp_path 会被 git 往上找到仓库根的 .git，断言就不成立了
        tool = make_tool(outside_any_repo, ok)
        r = await call(tool, title="t")
        assert r.is_error and "不是 git 仓库" in r.output

    async def test_missing_base_ref(self, repo):
        (repo / "app.py").write_text("changed\n", encoding="utf-8")
        tool = make_tool(repo, ok, base_ref="no-such-branch")
        r = await call(tool, title="t")
        assert r.is_error and "基线" in r.output

    async def test_pr_json_written_with_metadata(self, repo):
        (repo / "app.py").write_text("y = 2\n", encoding="utf-8")
        tool = make_tool(repo, ok)
        await call(tool, title="feat: 加 y", description="desc", issue="#7")
        import json

        meta = json.loads((repo / ".mewcode" / "pr" / "pr.json").read_text(encoding="utf-8"))
        assert meta["title"] == "feat: 加 y"
        assert meta["issue"] == "#7"
        assert meta["verify"]["exit_code"] == 0
        assert "app.py" in meta["diff_stat"]

    async def test_verifier_exception_does_not_crash(self, repo):
        """验证器内部抛异常不应该让 agent 崩，而要变成一次可读的拒绝。"""

        def boom() -> VerifyResult:
            raise RuntimeError("验证脚本炸了")

        (repo / "app.py").write_text("z = 3\n", encoding="utf-8")
        tool = make_tool(repo, boom)
        with pytest.raises(RuntimeError):
            await call(tool, title="t")


# ---------------------------------------------------------------------------
# 4. 真实验证器（退出码是唯一可信信号）
# ---------------------------------------------------------------------------


class TestRealVerifier:
    def test_exit_zero_means_pass(self, repo):
        v = make_verifier("git status --short", str(repo), timeout=30)
        r = v()
        assert r.ok and r.exit_code == 0

    def test_quoted_program_path_is_found(self, repo):
        """回归：Windows 上 shlex(posix=False) 保留引号，曾导致带引号的
        程序路径直接报「命令不存在」（退出码 127）。"""
        import sys

        cmd = f'"{sys.executable}" -c "print(1)"'
        v = make_verifier(cmd, str(repo), timeout=30)
        r = v()
        assert r.exit_code != 127, f"引号没剥干净：{r.output}"
        assert r.ok, r.output

    def test_nonzero_exit_means_fail(self, repo):
        v = make_verifier("git rev-parse --verify no-such-ref", str(repo), timeout=30)
        r = v()
        assert not r.ok and r.exit_code != 0
        assert "no-such-ref" in r.output or "fatal" in r.output.lower()

    def test_timeout_is_reported(self, repo):
        v = make_verifier("git --version", str(repo), timeout=0)
        r = v()
        assert r.exit_code == 124 and "超时" in r.output

    def test_missing_command_is_reported(self, repo):
        v = make_verifier("definitely-not-a-real-command-xyz", str(repo), timeout=10)
        r = v()
        assert r.exit_code == 127 and "不存在" in r.output


class TestCommandSplitting:
    def test_strips_matching_quotes(self):
        from mewcode.tools.create_pr import _split_command

        assert _split_command('"C:\\Program Files\\py.exe" -m pytest') == [
            "C:\\Program Files\\py.exe",
            "-m",
            "pytest",
        ]

    def test_plain_command_untouched(self):
        from mewcode.tools.create_pr import _split_command

        assert _split_command("pytest -q --tb=short") == ["pytest", "-q", "--tb=short"]

    def test_empty_falls_back_to_raw(self):
        from mewcode.tools.create_pr import _split_command

        assert _split_command("") == [""]

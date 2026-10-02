"""CI 侧脚本测试。

用真实 git 仓库 + 真实子进程调用（不走 mock），因为要验证的正是
"补丁能不能应用"「验证失败会不会推送」这类**真实副作用**。

重点守三条：
  1. 缺补丁 / 空补丁 / 脏补丁 → 必须失败并留痕
  2. **独立重跑失败 → 绝不推送**（这是"不信 agent 自己跑的那次"的落地）
  3. 派生分支名绝不等于基线分支
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CI_SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "apply_and_open_pr.py"


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    sh(d, "init", "-b", "main")
    sh(d, "config", "user.email", "t@e.com")
    sh(d, "config", "user.name", "T")
    (d / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    sh(d, "add", "-A")
    sh(d, "commit", "-m", "init")
    return d


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def write_patch(artifacts: Path, repo: Path, old: str, new: str,
                title: str = "fix: 改 VALUE", issue: str = "#1") -> None:
    """让仓库产生一个真实改动，再导出成补丁，模拟 agent 的产出。"""
    f = repo / "app.py"
    f.write_text(new, encoding="utf-8")
    subprocess.run(["git", "diff", "main"], cwd=repo, capture_output=True, text=True,
                   encoding="utf-8")
    r = subprocess.run(["git", "diff", "main"], cwd=repo, capture_output=True,
                       text=True, encoding="utf-8")
    (artifacts / "changes.patch").write_text(r.stdout, encoding="utf-8")
    (artifacts / "pr.json").write_text(
        json.dumps(
            {
                "title": title,
                "description": "把 VALUE 改掉",
                "issue": issue,
                "verify": {"command": "pytest -q", "exit_code": 0, "elapsed_s": 0.5},
                "independent_verify": {"verdict": "PASS", "reason": "ok"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 把仓库还原成补丁应用前的状态（CI 是在干净检出上 apply）
    sh(repo, "checkout", "--", "app.py")


def run_ci(repo: Path, artifacts: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CI_SCRIPT),
         "--repo", str(repo), "--artifacts", str(artifacts), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------------
# 1. 前置检查
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_missing_patch_fails_with_report(self, repo, artifacts):
        r = run_ci(repo, artifacts, "--dry-run")
        assert r.returncode != 0
        report = (artifacts / "FAILURE.md").read_text(encoding="utf-8")
        assert "读取补丁" in report and "不存在" in report

    def test_empty_patch_fails(self, repo, artifacts):
        (artifacts / "changes.patch").write_text("   \n", encoding="utf-8")
        r = run_ci(repo, artifacts, "--dry-run")
        assert r.returncode != 0
        assert "补丁为空" in (artifacts / "FAILURE.md").read_text(encoding="utf-8")

    def test_unapplicable_patch_fails_at_check_stage(self, repo, artifacts):
        (artifacts / "changes.patch").write_text(
            "diff --git a/nope.py b/nope.py\n"
            "--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n",
            encoding="utf-8",
        )
        r = run_ci(repo, artifacts, "--dry-run")
        assert r.returncode != 0
        assert "应用补丁" in (artifacts / "FAILURE.md").read_text(encoding="utf-8")

    def test_not_a_git_repo_fails(self, outside_any_repo, artifacts):
        # 用 outside_any_repo 而不是裸 tmp_path —— 本仓库自己就是 git checkout，
        # 裸 tmp_path 会被 git 往上找到仓库根的 .git，断言就不成立了
        (artifacts / "changes.patch").write_text("x", encoding="utf-8")
        r = run_ci(outside_any_repo, artifacts, "--dry-run")
        assert r.returncode != 0
        assert "不是 git 仓库" in (artifacts / "FAILURE.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. 独立重跑 —— 整条流水线的裁判
# ---------------------------------------------------------------------------


class TestIndependentReRun:
    def test_dry_run_applies_and_verifies(self, repo, artifacts):
        write_patch(artifacts, repo, "VALUE = 1", "VALUE = 2\n")
        r = run_ci(repo, artifacts, "--dry-run",
                   "--verify-cmd", f'"{sys.executable}" -c "print(1)"')
        assert r.returncode == 0, r.stderr
        # 补丁真的被应用了
        assert (repo / "app.py").read_text(encoding="utf-8").strip() == "VALUE = 2"

    def test_failed_reverify_blocks_everything(self, repo, artifacts):
        """★ 核心：agent 说验证过了，但 CI 重跑失败 → 什么都不推。"""
        write_patch(artifacts, repo, "VALUE = 1", "VALUE = 2\n")
        r = run_ci(repo, artifacts, "--dry-run",
                   "--verify-cmd", f'"{sys.executable}" -c "import sys; sys.exit(7)"')

        assert r.returncode != 0
        report = (artifacts / "FAILURE.md").read_text(encoding="utf-8")
        assert "独立验证" in report
        assert "退出码：7" in report
        # agent 自己的验证记录必须一起附上，方便对照"是不是它谎报了"
        assert "independent_verify" in report or '"verdict"' in report

    def test_failed_reverify_does_not_push(self, repo, artifacts, requires_real_push):
        """★ 重跑失败 → 远端零变化。

        `requires_real_push` 是控制组：先证明本环境真能 push，
        否则"远端零变化"无法区分"没推"和"推了但失败"。见 tests/conftest.py。
        """
        origin = repo.parent / "origin.git"
        origin.mkdir()
        sh(origin, "init", "--bare", "-b", "main")
        sh(repo, "remote", "add", "origin", str(origin))
        before = sh(origin, "rev-list", "--all", "--count").strip()

        write_patch(artifacts, repo, "VALUE = 1", "VALUE = 2\n")
        run_ci(repo, artifacts, "--verify-cmd",
               f'"{sys.executable}" -c "import sys; sys.exit(3)"')

        assert sh(origin, "rev-list", "--all", "--count").strip() == before
        assert sh(origin, "branch", "--list").strip() == ""

    def test_successful_reverify_actually_pushes(self, repo, artifacts, requires_real_push):
        """★ 对照实验：验证通过时，**必须真的推到远端**。

        没有这一条，上面那些"没推送"的断言全靠否定式证明 ——
        万一代码路径里根本没接 push，它们照样全绿。
        这条证明推送这一步是真的会发生，负向断言才有意义。
        """
        origin = repo.parent / "origin.git"
        origin.mkdir()
        sh(origin, "init", "--bare", "-b", "main")
        sh(repo, "remote", "add", "origin", str(origin))
        before = sh(origin, "rev-list", "--all", "--count").strip()

        write_patch(artifacts, repo, "VALUE = 1", "VALUE = 2\n")
        r = run_ci(repo, artifacts, "--no-pr",
                   "--verify-cmd", f'"{sys.executable}" -c "print(1)"')
        assert r.returncode == 0, r.stderr

        after = sh(origin, "rev-list", "--all", "--count").strip()
        assert after != before, "验证通过了却没推到远端 —— 推送这一步没接上"
        branches = sh(origin, "branch", "--list").strip()
        assert "agent/" in branches, f"远端分支名不对：{branches!r}"
        # 基线分支绝不能被推上去
        assert "main" not in branches.replace("*", "").split()

    def test_no_push_when_patch_produces_no_change(self, repo, artifacts):
        """补丁应用了但没产生实际改动（例如只改了空白）→ 不该提交空 commit。"""
        (artifacts / "changes.patch").write_text(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 1\n",
            encoding="utf-8",
        )
        r = run_ci(repo, artifacts,
                   "--verify-cmd", f'"{sys.executable}" -c "print(1)"')
        # git apply 可能直接拒绝这种补丁，也可能应用后无改动；两种情况都要失败
        assert r.returncode != 0


# ---------------------------------------------------------------------------
# 3. 分支名安全
# ---------------------------------------------------------------------------


class TestBranchSafety:
    def test_slugify_handles_chinese(self):
        from mewcode.delivery import slugify

        assert slugify("fix: 修导出乱码") == "fix"      # 中文被压掉，剩英文部分
        assert slugify("修复导出") == "change"           # 全中文 → 退回兜底
        assert slugify("Fix Export Encoding!") == "fix-export-encoding"

    def test_derived_branch_is_always_namespaced(self):
        """安全性来自**最终分支名**的 `agent/` 前缀 + sha 后缀，不是 slug 本身。

        `slugify("main")` 返回 `main` 是无害的 —— 最终分支是
        `agent/main-<sha>`，永远不可能等于基线 `main`。
        """
        from mewcode.delivery import derive_branch

        for title in ("main", "master", "修复", "agent/main", "", "  "):
            branch = derive_branch(title, "abc1234")
            assert branch.startswith("agent/"), branch
            assert branch not in ("main", "master"), branch
            assert branch.count("/") == 1, f"分支名结构异常: {branch}"

    def test_empty_or_symbol_only_title_falls_back(self):
        from mewcode.delivery import slugify

        assert slugify("") == "change"
        assert slugify("!!!") == "change"
        assert slugify("修复") == "change"

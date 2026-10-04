"""规范校验关卡的测试。

方向二要求「agent 写出来的代码天然符合内部标准」。光往 system prompt 里
塞规范是**软约束** —— 模型可能没读、读漏、或者读懂了但写完就忘。

所以内部规范被做成**可执行的检查命令**，接进 CreatePR 门禁：
退出码非 0 就不交付。这个文件守三件事：

  ① fail-closed —— 有一条不过就拒绝，且**不产出任何补丁**
  ② 排在昂贵验证**之前** —— 规范检查通常就是 lint/grep，最便宜，
     不该在它没过的情况下还去花一次独立验证
  ③ 报错信息要能让模型自己修 —— 带上命令、要求（hint）、失败输出
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.tools.create_pr import CreatePRTool, VerifyResult
from mewcode.validator import ConfigError, validate_standards, validate_toolset

PY = sys.executable


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-b", "main")
    git(d, "config", "user.email", "t@e.com")
    git(d, "config", "user.name", "T")
    (d / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-m", "init")
    return d


def ok_result() -> VerifyResult:
    return VerifyResult(ok=True, command="pytest -q", exit_code=0,
                        output="all good", elapsed=0.1)


class _Spy:
    """记录「命令验证」有没有被调用过 —— 用来证明顺序。"""

    def __init__(self, result: VerifyResult | None = None):
        self.calls = 0
        self._result = result or ok_result()

    def __call__(self) -> VerifyResult:
        self.calls += 1
        return self._result


def make_tool(repo: Path, *, standards, verify=None, verifier_runner=None, **kw):
    opts = {
        "work_dir": str(repo),
        "verify_command": "pytest -q",
        "artifacts_dir": str(repo / ".mewcode" / "pr"),
        "base_ref": "main",
        "verifier": verify or ok_result,
        "standards": standards,
        "mode": "patch",
    }
    if verifier_runner is not None:
        opts["verifier_runner"] = verifier_runner
        opts["require_independent"] = True
    opts.update(kw)
    return CreatePRTool(**opts)


async def call(tool: CreatePRTool, **kwargs):
    return await tool.execute(tool.params_model(**kwargs))


def patch_path(repo: Path) -> Path:
    return repo / ".mewcode" / "pr" / "changes.patch"


# ---------------------------------------------------------------------------
# 一、基本语义
# ---------------------------------------------------------------------------


class TestStandardsBasics:
    async def test_no_standards_behaves_as_before(self, repo):
        """不配 standards 时行为完全不变 —— 这是刻意的（默认不变）。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, standards=[])
        r = await call(tool, title="t")
        assert not r.is_error, r.output
        assert patch_path(repo).exists()

    async def test_passing_standard_allows_delivery(self, repo):
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, standards=[
            {"name": "always-ok", "command": f'"{PY}" -c "print(1)"'},
        ])
        r = await call(tool, title="t")
        assert not r.is_error, r.output
        assert patch_path(repo).exists()

    async def test_failing_standard_blocks_and_produces_no_patch(self, repo):
        """★ fail-closed：规范不过就**不产出任何补丁**。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, standards=[
            {"name": "forbid-print", "command": f'"{PY}" -c "import sys; sys.exit(3)"',
             "hint": "业务代码里禁止 print，请用 logger"},
        ])
        r = await call(tool, title="t")
        assert r.is_error
        assert "规范校验" in r.output
        assert not patch_path(repo).exists(), "规范没过却产出了补丁"

    async def test_error_message_carries_command_hint_and_output(self, repo):
        """报错要能让模型自己修 —— 命令 / 要求 / 输出三样都要有。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, standards=[{
            "name": "import-order",
            "command": f'"{PY}" -c "import sys; print(\'E401 导入顺序错\'); sys.exit(1)"',
            "hint": "按 isort 规则排序导入",
        }])
        r = await call(tool, title="t")
        assert "import-order" in r.output
        assert "按 isort 规则排序导入" in r.output
        assert "E401 导入顺序错" in r.output

    async def test_reports_failure_count(self, repo):
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        fail = f'"{PY}" -c "import sys; sys.exit(1)"'
        tool = make_tool(repo, standards=[
            {"name": "a", "command": fail},
            {"name": "b", "command": fail},
            {"name": "c", "command": f'"{PY}" -c "print(1)"'},
        ])
        r = await call(tool, title="t")
        assert "2/3" in r.output
        # 三条的名字都要能看到，方便定位
        assert "【a】" in r.output and "【b】" in r.output
        assert "【c】" not in r.output

    async def test_all_standards_run_even_if_first_fails(self, repo):
        """第一条挂了也要跑完剩下的 —— 一次性告诉模型所有问题，
        而不是修一个跑一次、来回好几轮。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        marker = repo / "second_ran.txt"
        tool = make_tool(repo, standards=[
            {"name": "first", "command": f'"{PY}" -c "import sys; sys.exit(1)"'},
            {"name": "second",
             "command": f'"{PY}" -c "open(r\'{marker}\',\'w\').write(\'x\')"'},
        ])
        await call(tool, title="t")
        assert marker.exists(), "第一条失败后第二条没跑"


# ---------------------------------------------------------------------------
# 二、顺序：规范检查排在昂贵验证之前 ★
# ---------------------------------------------------------------------------


class TestOrdering:
    async def test_standards_run_before_command_verify(self, repo):
        """★ 规范检查最便宜（lint/grep），该排在命令验证之前。

        否则会在「先违反了内部规范」的情况下还去跑一遍完整测试套件。
        """
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        spy = _Spy()
        tool = make_tool(
            repo,
            standards=[{"name": "s", "command": f'"{PY}" -c "import sys; sys.exit(1)"'}],
            verify=spy,
        )
        r = await call(tool, title="t")
        assert r.is_error
        assert spy.calls == 0, "规范没过却已经跑了命令验证"

    async def test_standards_run_before_independent_verifier(self, repo):
        """★ 更重要的是排在**独立验证者**之前 —— 那是最贵的一层
        （要跑一整个 agent 循环）。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        called = {"n": 0}

        async def verifier(_desc: str) -> str:
            called["n"] += 1
            return "VERDICT: PASS"

        tool = make_tool(
            repo,
            standards=[{"name": "s", "command": f'"{PY}" -c "import sys; sys.exit(1)"'}],
            verifier_runner=verifier,
        )
        r = await call(tool, title="t")
        assert r.is_error
        assert called["n"] == 0, "规范没过却已经调用了独立验证者"

    async def test_standards_run_after_repo_check(self, outside_any_repo):
        """但要在「是不是 git 仓库」之后 —— 那是能不能干活的前提。

        用 `outside_any_repo` fixture（conftest.py）：裸 tmp_path 位于
        `<仓库>/.eval-tmp/` 下，`git rev-parse` 会一路往上找到仓库根的 .git，
        于是它其实是"在仓库里"，这个断言就不成立了。
        """
        tool = make_tool(outside_any_repo, standards=[
            {"name": "s", "command": f'"{PY}" -c "import sys; sys.exit(1)"'},
        ])
        r = await call(tool, title="t")
        assert r.is_error
        assert "不是 git 仓库" in r.output


# ---------------------------------------------------------------------------
# 三、校验
# ---------------------------------------------------------------------------


class TestValidateStandards:
    def test_none_and_empty(self):
        assert validate_standards(None) == []
        assert validate_standards([]) == []

    def test_full_form(self):
        out = validate_standards([
            {"name": "no-print", "command": "ruff check .", "hint": "别用 print"}
        ])
        assert out == [{"name": "no-print", "command": "ruff check .", "hint": "别用 print"}]

    def test_string_shorthand(self):
        """写一个字符串就等于「名字和命令都是它」—— 省得为简单检查写三行。"""
        out = validate_standards(["ruff check ."])
        assert out == [{"name": "ruff check .", "command": "ruff check .", "hint": ""}]

    def test_missing_name(self):
        with pytest.raises(ConfigError, match="name"):
            validate_standards([{"command": "x"}])

    def test_missing_command(self):
        with pytest.raises(ConfigError, match="command"):
            validate_standards([{"name": "a"}])

    def test_duplicate_name(self):
        with pytest.raises(ConfigError, match="重复"):
            validate_standards([
                {"name": "a", "command": "x"}, {"name": "a", "command": "y"},
            ])

    def test_bad_container(self):
        with pytest.raises(ConfigError, match="must be a list"):
            validate_standards("ruff check .")

    def test_bad_item(self):
        with pytest.raises(ConfigError, match="mapping"):
            validate_standards([123])

    def test_hint_must_be_string(self):
        with pytest.raises(ConfigError, match="hint"):
            validate_standards([{"name": "a", "command": "x", "hint": 1}])

    def test_wired_through_toolset(self):
        out = validate_toolset({
            "create_pr": {
                "enabled": True, "verify_command": "pytest -q",
                "standards": [{"name": "n", "command": "c"}],
            }
        })
        assert out["create_pr"]["standards"] == [{"name": "n", "command": "c", "hint": ""}]

    def test_default_empty(self):
        out = validate_toolset({"create_pr": {"enabled": True, "verify_command": "x"}})
        assert out["create_pr"]["standards"] == []


# ---------------------------------------------------------------------------
# 四、真实命令的端到端（不是 python -c 的桩）
# ---------------------------------------------------------------------------


class TestRealCommands:
    async def test_grep_based_standard_catches_a_real_violation(self, repo):
        """用真实命令（findstr/find 风格的 grep）演示典型用法：
        禁止在业务代码里出现 print(。"""
        (repo / "app.py").write_text(
            "import logging\nlogger = logging.getLogger(__name__)\nprint('debug')\n",
            encoding="utf-8",
        )
        # 用 python 自己写检查，避免依赖 grep 是否存在于 PATH
        checker = (
            f'"{PY}" -c "import pathlib,sys; '
            "bad=[p for p in pathlib.Path('.').rglob('*.py') "
            "if 'print(' in p.read_text(encoding='utf-8')]; "
            "print('发现 print:', bad); sys.exit(1 if bad else 0)\""
        )
        tool = make_tool(repo, standards=[
            {"name": "no-print-in-business-code", "command": checker,
             "hint": "业务代码用 logging，不要用 print"},
        ])
        r = await call(tool, title="t")
        assert r.is_error
        assert "no-print-in-business-code" in r.output
        assert "发现 print" in r.output

    async def test_same_standard_passes_after_fix(self, repo):
        checker = (
            f'"{PY}" -c "import pathlib,sys; '
            "bad=[p for p in pathlib.Path('.').rglob('*.py') "
            "if 'print(' in p.read_text(encoding='utf-8')]; "
            "sys.exit(1 if bad else 0)\""
        )
        (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        tool = make_tool(repo, standards=[
            {"name": "no-print", "command": checker},
        ])
        r = await call(tool, title="t")
        assert not r.is_error, r.output

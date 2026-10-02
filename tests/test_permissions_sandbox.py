"""路径沙箱与权限确认的越权回归测试。

这里的每条用例都对应一个**实测确认过的越权路径**，修复后作为护栏存在：

  E1  顺序问题：安全只读命令白名单排在沙箱之前 → `cat /etc/hosts` 从未被沙箱校验
  E2  Glob/Grep 字段错位：只校验 `pattern`，而决定"搜哪里"的是 `path`
  E3  类型排除：command 类工具被 Layer 2 整个排除 → Bash 可写沙箱外任意路径
  E4  规则粒度：ALLOW_ALWAYS 对无作用域字段的工具生成 `"*"` 通配规则 = 永久全放行

同时用 R1~R5 守住反面：正常操作**不得**被误拦（否则沙箱会被用户关掉，等于没有）。
"""
from __future__ import annotations

import pytest

from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.rules import extract_command_paths, extract_content
from mewcode.tools import create_default_registry


@pytest.fixture(scope="module")
def work_dir(tmp_path_factory=None) -> str:
    # 用项目根当沙箱根，避免依赖 pytest 的 tmp 路径
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent)


@pytest.fixture(scope="module")
def checker(work_dir: str) -> PermissionChecker:
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(),          # 空规则，排除规则引擎干扰
        mode=PermissionMode.DEFAULT,       # read=allow / write=ask / command=ask
    )


@pytest.fixture(scope="module")
def registry():
    return create_default_registry()


def decide(checker, registry, tool_name, arguments):
    tool = registry.get(tool_name)
    assert tool is not None, tool_name
    return checker.check(tool, arguments)


# ---------------------------------------------------------------------------
# E1~E3：越权必须被拦
# ---------------------------------------------------------------------------


class TestEscalation:
    """每条都是一个真实的越权路径，修复后必须 deny。"""

    def test_e3_command_tool_cannot_write_outside(self, checker, registry):
        """E3：Bash 曾因 category=="command" 被沙箱整层排除。"""
        d = decide(checker, registry, "Bash",
                   {"command": "echo pwned > C:/Windows/Temp/pwned.txt"})
        assert d.effect == "deny"
        assert "路径沙箱拦截" in d.reason

    def test_e1_safe_readonly_cannot_shortcircuit_sandbox(self, checker, registry):
        """E1：`cat /etc/hosts` 命中只读白名单，曾以 allow 直接短路掉沙箱。

        作用域是硬约束，不该被"这条命令只读所以安全"的便利判断绕过 ——
        只读不等于在范围内。
        """
        d = decide(checker, registry, "Bash", {"command": "cat /etc/hosts"})
        assert d.effect == "deny", f"只读命令越界未被拦: {d.effect} / {d.reason}"
        assert "/etc/hosts" in d.reason

    def test_e1_sensitive_file_read_blocked(self, checker, registry):
        d = decide(checker, registry, "Bash", {"command": "head -n 5 /etc/shadow"})
        assert d.effect == "deny"

    def test_e2_glob_checks_path_not_pattern(self, checker, registry):
        """E2：Glob 曾只把 `pattern` 送进沙箱，`path` 完全没校验。"""
        d = decide(checker, registry, "Glob",
                   {"pattern": "*.ini", "path": "C:/Windows"})
        assert d.effect == "deny"
        assert "C:/Windows" in d.reason

    def test_e2_grep_checks_path_not_pattern(self, checker, registry):
        d = decide(checker, registry, "Grep",
                   {"pattern": "password", "path": "C:/Users"})
        assert d.effect == "deny"

    def test_read_outside_is_denied(self, checker, registry):
        d = decide(checker, registry, "ReadFile",
                   {"file_path": "C:/Windows/win.ini"})
        assert d.effect == "deny"

    def test_write_outside_is_denied(self, checker, registry):
        d = decide(checker, registry, "WriteFile",
                   {"file_path": "C:/Windows/Temp/e.txt"})
        assert d.effect == "deny"

    def test_relative_path_escape_is_denied(self, checker, registry):
        d = decide(checker, registry, "WriteFile",
                   {"file_path": "../../outside.txt"})
        assert d.effect == "deny"

    def test_symlink_escape_is_resolved(self, checker, registry, tmp_path):
        """软链指向沙箱外时，resolve(strict=True) 应拿物理路径后拦截。"""
        outside = tmp_path.parent / "outside_target.txt"
        outside.write_text("secret", encoding="utf-8")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("当前平台/权限不支持创建符号链接")
        d = decide(checker, registry, "ReadFile", {"file_path": str(link)})
        assert d.effect == "deny"


# ---------------------------------------------------------------------------
# E4：规则粒度
# ---------------------------------------------------------------------------


class TestRuleGranularity:
    """E4：`extract_content` 对无作用域字段的工具返回空串，
    拼出来就是 `"*"`，`fnmatch(任意, "*")` 恒真 → 一次"别问了"= 永久全放行。

    修复方式：拿不到作用域字段就不落规则；调用方（agent.py）据此跳过写入。
    """

    @pytest.mark.parametrize("tool_name,arguments", [
        ("Agent", {"prompt": "do something"}),
        ("TeamCreate", {"team_name": "t"}),
    ])
    def test_scope_less_tools_yield_empty_content(self, tool_name, arguments):
        """空串是"无法收敛作用域"的信号，调用方必须据此拒绝落规则。"""
        assert extract_content(tool_name, arguments) == ""

    def test_scoped_tools_yield_real_content(self, work_dir):
        assert extract_content("Bash", {"command": "pytest -q"}) == "pytest -q"
        assert extract_content("WriteFile", {"file_path": f"{work_dir}/a.py"})

    def test_empty_content_would_have_produced_wildcard(self):
        """把这个退化过程写死成断言，防止有人把调用方的守卫删掉。"""
        content = extract_content("Agent", {"prompt": "x"})
        pattern = f"{content[:60]}*" if len(content) > 60 else f"{content}*"
        assert pattern == "*", "前提变了：extract_content 对 Agent 不再返回空串"

    def test_glob_primary_field_is_path(self):
        """Glob/Grep 的主字段必须是 path —— 它才是指向文件系统的那个。"""
        content = extract_content("Glob", {"path": "src", "pattern": "*.py"})
        assert content == "src"


# ---------------------------------------------------------------------------
# R1~R5：正常操作不得被误拦
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    """沙箱的可用性和它的严格性同等重要 —— 误拦会导致用户直接把它关掉。"""

    def test_read_inside_is_allowed(self, checker, registry, work_dir):
        d = decide(checker, registry, "ReadFile",
                   {"file_path": f"{work_dir}/app.py"})
        assert d.effect == "allow"

    def test_glob_default_path_is_allowed(self, checker, registry):
        d = decide(checker, registry, "Glob", {"pattern": "**/*.py", "path": "."})
        assert d.effect == "allow"

    def test_plain_readonly_commands_still_allowed(self, checker, registry):
        for cmd in ("ls -la memory/", "git log --oneline -5", "pwd"):
            d = decide(checker, registry, "Bash", {"command": cmd})
            assert d.effect == "allow", f"{cmd} 被误拦: {d.reason}"

    def test_https_url_is_not_mistaken_for_a_drive_path(self, checker, registry):
        """回归：`https:/` 里的 `s:/` 曾被当成盘符 S:，把正常网络命令判成越权。"""
        d = decide(checker, registry, "Bash",
                   {"command": "curl https://api.deepseek.com/anthropic"})
        assert d.effect == "ask", f"URL 被误判为路径: {d.reason}"

    def test_dev_null_is_ignored(self, checker, registry):
        d = decide(checker, registry, "Bash",
                   {"command": "find . -name '*.py' > /dev/null"})
        assert d.effect == "ask"

    def test_mkdir_inside_sandbox_not_denied(self, checker, registry, work_dir):
        d = decide(checker, registry, "Bash",
                   {"command": f"mkdir -p {work_dir}/.tmp-x"})
        assert d.effect == "ask"


# ---------------------------------------------------------------------------
# 路径提取器单测
# ---------------------------------------------------------------------------


class TestCommandPathExtraction:
    def test_extracts_posix_absolute(self):
        assert extract_command_paths("cat /etc/hosts") == ["/etc/hosts"]

    def test_extracts_windows_absolute(self):
        assert extract_command_paths("echo x > C:/Windows/Temp/a.txt") == [
            "C:/Windows/Temp/a.txt"
        ]

    def test_ignores_url_scheme(self):
        assert extract_command_paths("curl https://api.deepseek.com/x") == []

    def test_ignores_relative_paths(self):
        assert extract_command_paths("git log --oneline") == []
        assert extract_command_paths("find . -name '*.py'") == []

    def test_ignores_device_paths(self):
        assert extract_command_paths("cmd > /dev/null") == []

    def test_handles_empty(self):
        assert extract_command_paths("") == []

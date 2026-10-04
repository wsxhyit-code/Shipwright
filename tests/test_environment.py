"""环境管理工具的测试。

## 这个模块存在的理由，本身就是一组取舍

方向二的原文是「它跑在 Docker 容器里，**自己起测试环境**验证」。
但照字面实现会打破它自己的沙箱：

    起容器需要 docker 权限 → 挂 docker.sock → agent 能
    `docker run --privileged -v /:/host` 拿到宿主 root。

所以实现成了：**能起环境，但只能执行平台给定的那几条命令**。

这个文件要守的：

  ① 平台没给的动作 → 明确报错，不能假装成功
  ② 破坏性动作（stop / reset）在**代码里**拦，不靠提示词
  ③ 起环境失败时，要明确阻止「继续跑测试并相信结果」
  ④ 没配置就不注册工具（不改变默认行为）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mewcode.config import ToolsetConfig
from mewcode.toolset import assemble_toolset
from mewcode.tools import create_default_registry
from mewcode.tools.environment import (
    EnvironmentRunner,
    describe_environment,
    register_env_tools,
    split_command,
)
from mewcode.validator import ConfigError, validate_environment, validate_toolset

PY = sys.executable


def cmd(code: int = 0, text: str = "ok") -> str:
    return f'"{PY}" -c "import sys; print({text!r}); sys.exit({code})"'


CFG = {
    "start": cmd(0, "env up"),
    "status": cmd(0, "env ready"),
    "stop": cmd(0, "env down"),
    "reset": cmd(0, "env reset"),
    "allow_stop": False,
    "allow_reset": False,
    "timeout": 60,
    "settle_seconds": 0,
}


# ---------------------------------------------------------------------------
# 一、命令拆分（老坑：Windows 上引号会被保留）
# ---------------------------------------------------------------------------


class TestSplitCommand:
    def test_strips_quotes_from_program_path(self):
        """`shlex.split(posix=False)` 会保留引号，导致程序路径带引号、退出码 127。"""
        got = split_command('"C:\\Program Files\\py.exe" -m pytest -q')
        assert got[0] == "C:\\Program Files\\py.exe"
        assert got[1:] == ["-m", "pytest", "-q"]

    def test_plain_command(self):
        assert split_command("bash up.sh") == ["bash", "up.sh"]

    def test_unbalanced_quote_falls_back(self):
        assert split_command('bash "unclosed') == ['bash "unclosed']


# ---------------------------------------------------------------------------
# 二、正常路径
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_start_runs_the_configured_command(self):
        o = EnvironmentRunner(CFG).run("start")
        assert o.ok, o.output
        assert "env up" in o.output
        assert o.action == "start"

    def test_status_is_reported(self):
        o = EnvironmentRunner(CFG).run("status")
        assert o.ok
        assert "env ready" in o.output

    def test_settle_seconds_is_honoured(self):
        """有些平台脚本起完服务需要等一下才 ready。"""
        import time

        cfg = {**CFG, "settle_seconds": 1}
        t0 = time.monotonic()
        EnvironmentRunner(cfg).run("start")
        assert time.monotonic() - t0 >= 0.9

    def test_settle_not_applied_on_failure(self):
        import time

        cfg = {**CFG, "start": cmd(1, "boom"), "settle_seconds": 5}
        t0 = time.monotonic()
        EnvironmentRunner(cfg).run("start")
        assert time.monotonic() - t0 < 4, "失败时不该还傻等"


# ---------------------------------------------------------------------------
# 三、平台没给的动作
# ---------------------------------------------------------------------------


class TestMissingAction:
    def test_missing_action_is_a_clear_error(self):
        cfg = {"start": cmd()}
        o = EnvironmentRunner(cfg).run("reset")
        assert not o.ok
        assert "平台没有提供" in o.denied_reason
        assert "docker" in o.denied_reason      # 明确堵住"自己拼命令"的念头

    def test_lists_what_is_available(self):
        o = EnvironmentRunner({"start": cmd(), "status": cmd()}).run("stop")
        assert "start" in o.denied_reason
        assert "status" in o.denied_reason

    def test_empty_config_lists_nothing(self):
        o = EnvironmentRunner({}).run("start")
        assert "一个都没配" in o.denied_reason


# ---------------------------------------------------------------------------
# 四、破坏性动作在代码里拦 ★
# ---------------------------------------------------------------------------


class TestDestructiveGates:
    def test_stop_denied_by_default(self):
        """★ `allow_stop` 默认关，且**在代码里拦** —— 不靠提示词劝阻。"""
        o = EnvironmentRunner(CFG).run("stop")
        assert not o.ok
        assert "allow_stop" in o.denied_reason
        assert "env down" not in o.output, "被拒了却还是执行了"

    def test_reset_denied_by_default(self):
        o = EnvironmentRunner(CFG).run("reset")
        assert not o.ok
        assert "allow_reset" in o.denied_reason

    def test_stop_allowed_when_enabled(self):
        o = EnvironmentRunner({**CFG, "allow_stop": True}).run("stop")
        assert o.ok

    def test_reset_allowed_when_enabled(self):
        o = EnvironmentRunner({**CFG, "allow_reset": True}).run("reset")
        assert o.ok

    def test_gate_survives_command_present(self):
        """命令配了但开关没开 —— 依然拒绝。这才是门禁的意义。"""
        cfg = {"stop": cmd(0, "should not run"), "allow_stop": False}
        o = EnvironmentRunner(cfg).run("stop")
        assert not o.ok
        assert "should not run" not in o.output


# ---------------------------------------------------------------------------
# 五、失败路径
# ---------------------------------------------------------------------------


class TestFailures:
    def test_nonzero_exit_is_reported_with_output(self):
        cfg = {"start": cmd(3, "port already in use")}
        o = EnvironmentRunner(cfg).run("start")
        assert not o.ok
        assert o.exit_code == 3
        assert "port already in use" in o.output

    def test_timeout(self):
        cfg = {"start": f'"{PY}" -c "import time; time.sleep(30)"', "timeout": 1}
        o = EnvironmentRunner(cfg).run("start")
        assert not o.ok
        assert o.exit_code == 124

    def test_missing_program(self):
        o = EnvironmentRunner({"start": "definitely-not-a-real-program-xyz"}).run("start")
        assert not o.ok
        assert "命令不存在" in o.output


# ---------------------------------------------------------------------------
# 六、工具层
# ---------------------------------------------------------------------------


class TestTools:
    def _reg(self, cfg):
        reg = create_default_registry()
        register_env_tools(reg, cfg)
        return reg

    async def _call(self, reg, name, **kw):
        t = reg.get(name)
        assert t is not None, f"{name} 没注册"
        return await t.execute(t.params_model(**kw))

    async def test_start_tool_success(self):
        r = await self._call(self._reg(CFG), "StartTestEnv")
        assert not r.is_error
        assert "env up" in r.output

    async def test_start_tool_failure_tells_model_not_to_trust_tests(self):
        """★ 环境没起来时，必须明确阻止「继续跑测试并相信结果」。"""
        reg = self._reg({"start": cmd(1, "db refused")})
        r = await self._call(reg, "StartTestEnv")
        assert r.is_error
        assert "不要在这种情况下继续跑测试" in r.output

    async def test_stop_tool_denied_by_default(self):
        r = await self._call(self._reg(CFG), "StopTestEnv")
        assert r.is_error
        assert "allow_stop" in r.output

    async def test_reset_tool_denied_by_default(self):
        r = await self._call(self._reg(CFG), "ResetTestEnv")
        assert r.is_error
        assert "allow_reset" in r.output

    async def test_env_status_is_read_category(self):
        assert self._reg(CFG).get("EnvStatus").category == "read"

    async def test_write_tools_are_command_category(self):
        reg = self._reg(CFG)
        for n in ("StartTestEnv", "StopTestEnv", "ResetTestEnv"):
            assert reg.get(n).category == "command", n

    async def test_no_backend_error_mentions_not_connected(self):
        """没接入时要说明"不代表环境不需要起"，且别自己猜连接串。"""
        reg = create_default_registry()
        register_env_tools(reg, {"start": cmd()})
        # 用一个没有 runner 的情况：直接构造工具传 None
        from mewcode.tools.environment import StartTestEnvTool

        r = await StartTestEnvTool(None).execute(StartTestEnvTool.params_model())
        assert r.is_error
        assert "不代表" in r.output
        assert "不要自己猜连接串" in r.output


# ---------------------------------------------------------------------------
# 七、能力清单
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_empty_when_nothing_configured(self):
        assert describe_environment(None) == ""
        assert describe_environment({}) == ""

    def test_lists_actions_and_gates(self):
        text = describe_environment(CFG)
        assert "start" in text and "status" in text
        assert "allow_stop" in text      # 标出被禁的动作
        assert "allow_reset" in text

    def test_says_no_docker(self):
        """必须明说容器里没有 docker —— 否则模型会去试。"""
        assert "docker" in describe_environment(CFG)


# ---------------------------------------------------------------------------
# 八、装配与校验
# ---------------------------------------------------------------------------


class TestWiring:
    def test_not_configured_registers_nothing(self, tmp_path):
        reg = create_default_registry()
        before = {t.name for t in reg.list_tools()}
        assemble_toolset(reg, ToolsetConfig(), agent=None, work_dir=tmp_path)
        assert {t.name for t in reg.list_tools()} == before

    def test_configured_registers_four_tools(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.environment = {"start": cmd(), "status": cmd()}
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        names = {t.name for t in reg.list_tools()}
        for n in ("StartTestEnv", "StopTestEnv", "ResetTestEnv", "EnvStatus"):
            assert n in names
        assert "环境管理" in asm.prompt_note

    def test_register_helper_returns_empty_without_actions(self):
        reg = create_default_registry()
        assert register_env_tools(reg, None) == []
        assert register_env_tools(reg, {}) == []


class TestValidateEnvironment:
    def test_absent_is_none(self):
        assert validate_environment(None) is None
        assert validate_toolset(None)["environment"] is None

    def test_minimal_start(self):
        out = validate_environment({"start": "bash up.sh"})
        assert out["start"] == "bash up.sh"
        assert out["allow_stop"] is False
        assert out["allow_reset"] is False
        assert out["timeout"] == 600

    def test_empty_block_rejected(self):
        """配了环境段却一条命令都没有 —— 那是配置写错了，报错比忽略好。"""
        with pytest.raises(ConfigError, match="一条命令都没有"):
            validate_environment({})

    def test_allow_without_command_rejected(self):
        with pytest.raises(ConfigError, match="allow_reset"):
            validate_environment({"start": "x", "allow_reset": True})
        with pytest.raises(ConfigError, match="allow_stop"):
            validate_environment({"start": "x", "allow_stop": True})

    def test_non_string_command(self):
        with pytest.raises(ConfigError, match="must be a string"):
            validate_environment({"start": ["bash", "up.sh"]})

    def test_bad_timeout(self):
        with pytest.raises(ConfigError, match="timeout"):
            validate_environment({"start": "x", "timeout": 0})

    def test_settle_seconds_allows_zero(self):
        assert validate_environment({"start": "x", "settle_seconds": 0})["settle_seconds"] == 0

    def test_wired_through_toolset(self):
        out = validate_toolset({"environment": {"start": "bash up.sh", "allow_stop": True,
                                               "stop": "bash down.sh"}})
        assert out["environment"]["allow_stop"] is True

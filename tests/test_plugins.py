"""插件机制的测试。

要证明的核心：**第三方装个包就能加工具，不用改核心代码**；而且
**坏插件不能拖垮 agent，但也绝不能静默失败**。

（"悄悄少了一批工具"是最坏的结果 —— agent 不知道自己没有那些能力，
会去猜、或者用别的方式硬凑。所以每个失败路径都要能在能力清单里看见。）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mewcode.config import ToolsetConfig
from mewcode.toolset import assemble_toolset
from mewcode.tools import create_default_registry
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.plugins import (
    ENTRY_POINT_GROUP,
    PluginLoadResult,
    load_plugin_tools,
)
from mewcode.validator import ConfigError, validate_toolset


class _FakeEP:
    """假的 entry point：`load()` 返回给定对象或抛给定异常。"""

    def __init__(self, name: str, obj: Any = None, exc: Exception | None = None):
        self.name = name
        self._obj = obj
        self._exc = exc
        self.loaded = False

    def load(self):
        self.loaded = True
        if self._exc is not None:
            raise self._exc
        return self._obj


class _PingTool(Tool):
    name = "PingTool"
    description = "demo"
    params_model = None  # type: ignore[assignment]
    category = "read"

    async def execute(self, params) -> ToolResult:  # pragma: no cover
        return ToolResult(output="pong")


class _PongTool(Tool):
    name = "PongTool"
    description = "demo"
    params_model = None  # type: ignore[assignment]
    category = "read"

    async def execute(self, params) -> ToolResult:  # pragma: no cover
        return ToolResult(output="ping")


def _register_ping(registry) -> list[str]:
    registry.register(_PingTool())
    return ["PingTool"]


# ---------------------------------------------------------------------------
# 一、契约
# ---------------------------------------------------------------------------


class TestGroupName:
    def test_group_name_is_stable(self):
        """组名是对外契约 —— 改了所有第三方包都会静默失效。"""
        assert ENTRY_POINT_GROUP == "mewcode.tools"


class TestHappyPath:
    def test_registers_tools(self):
        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("acme", _register_ping)])
        assert r.loaded == ["acme"]
        assert r.tools == ["PingTool"]
        assert reg.get("PingTool") is not None
        assert r.ok

    def test_multiple_plugins(self):
        reg = create_default_registry()
        r = load_plugin_tools(reg, [
            _FakeEP("a", _register_ping),
            _FakeEP("b", lambda reg: (reg.register(_PongTool()), ["PongTool"])[1]),
        ])
        assert r.loaded == ["a", "b"]
        assert sorted(r.tools) == ["PingTool", "PongTool"]

    def test_register_may_return_none(self):
        """返回值是**可选**的 —— 插件不返回名字也算成功（只要能看出加了工具）。"""
        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("a", lambda reg: reg.register(_PingTool()))])
        assert r.loaded == ["a"]
        assert r.tools == ["PingTool"]

    def test_disabled_does_nothing(self):
        reg = create_default_registry()
        ep = _FakeEP("a", _register_ping)
        r = load_plugin_tools(reg, [ep], enabled=False)
        assert r.loaded == [] and r.tools == []
        assert not ep.loaded, "禁用时不该去 load entry point"
        assert reg.get("PingTool") is None


# ---------------------------------------------------------------------------
# 二、失败路径必须可见
# ---------------------------------------------------------------------------


class TestFailures:
    def test_import_failure_recorded_not_raised(self):
        """插件 import 挂了不能让 agent 起不来 —— 但要记下来。"""
        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("broken", exc=ImportError("no module named x"))])
        assert r.loaded == []
        assert not r.ok
        name, why = r.errors[0]
        assert name == "broken"
        assert "ImportError" in why

    def test_non_callable_entry_point(self):
        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("weird", "不是一个函数")])
        assert not r.ok
        assert "可调用对象" in r.errors[0][1]

    def test_register_raises_is_recorded(self):
        def boom(registry):
            raise RuntimeError("插件内部炸了")

        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("boom", boom)])
        assert not r.ok
        assert "RuntimeError" in r.errors[0][1]

    def test_register_adds_nothing_is_treated_as_failure(self):
        """★ 注册"成功"但一个工具都没加，大概率是插件写错了。

        记成"加载成功"会让一次静默失效溜过去 —— agent 以为有了工具，
        实际没有。
        """
        reg = create_default_registry()
        r = load_plugin_tools(reg, [_FakeEP("empty", lambda reg: None)])
        assert not r.ok
        assert "没有注册任何工具" in r.errors[0][1]

    def test_one_bad_plugin_does_not_block_others(self):
        reg = create_default_registry()
        r = load_plugin_tools(reg, [
            _FakeEP("bad", exc=ImportError("boom")),
            _FakeEP("good", _register_ping),
        ])
        assert r.loaded == ["good"]
        assert len(r.errors) == 1
        assert reg.get("PingTool") is not None


# ---------------------------------------------------------------------------
# 三、描述要能让模型看见风险
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_empty_result_describes_nothing(self):
        assert PluginLoadResult().describe() == ""

    def test_success_line(self):
        r = PluginLoadResult(loaded=["a"], tools=["T1"])
        text = r.describe()
        assert "已加载 1 个" in text
        assert "新增 1 个工具" in text

    def test_error_block_warns_that_tools_do_not_exist(self):
        r = PluginLoadResult(errors=[("bad", "ImportError: x")])
        text = r.describe()
        assert "加载失败" in text
        assert "✗ bad" in text
        assert "ImportError: x" in text
        # 关键：要说明"不是那些系统里没数据"，否则模型会去猜
        assert "不代表" in text

    def test_ok_property(self):
        assert PluginLoadResult(loaded=["a"]).ok
        assert not PluginLoadResult(errors=[("a", "x")]).ok


# ---------------------------------------------------------------------------
# 四、装配集成
# ---------------------------------------------------------------------------


class TestWiring:
    def test_not_enabled_changes_nothing(self, tmp_path, monkeypatch):
        """默认 plugins=False —— 不装包的普通用户行为完全不变。"""
        import mewcode.tools.plugins as P

        called = {"n": 0}

        def spy():
            called["n"] += 1
            return []

        monkeypatch.setattr(P, "discover_entry_points", spy)
        reg = create_default_registry()
        before = {t.name for t in reg.list_tools()}
        asm = assemble_toolset(reg, ToolsetConfig(), agent=None, work_dir=tmp_path)
        assert {t.name for t in reg.list_tools()} == before
        assert asm.plugins is None
        assert called["n"] == 0

    def test_enabled_with_no_plugins_is_not_an_error(self, tmp_path, monkeypatch):
        import mewcode.tools.plugins as P

        monkeypatch.setattr(P, "discover_entry_points", lambda: [])
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.plugins = True
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert asm.plugins is not None
        assert asm.plugins.loaded == []
        assert asm.plugins.ok

    def test_enabled_registers_plugin_tools(self, tmp_path, monkeypatch):
        import mewcode.tools.plugins as P

        monkeypatch.setattr(P, "discover_entry_points", lambda: [_FakeEP("a", _register_ping)])
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.plugins = True
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "PingTool" in asm.tool_names
        assert reg.get("PingTool") is not None

    def test_failed_plugin_shows_up_in_prompt_note(self, tmp_path, monkeypatch):
        """★ 坏插件必须在**给模型看的能力清单**里出现。

        否则就是"悄悄少了一批工具" —— 最坏的结果。
        """
        import mewcode.tools.plugins as P

        monkeypatch.setattr(
            P, "discover_entry_points",
            lambda: [_FakeEP("acme", exc=ImportError("no acme sdk"))],
        )
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.plugins = True
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "acme" in asm.prompt_note
        assert "加载失败" in asm.prompt_note
        assert "不代表" in asm.prompt_note

    def test_plugin_note_survives_other_sections(self, tmp_path, monkeypatch):
        """★ 三段说明必须共存。

        早先运维那段写的是 `result.prompt_note = report`（赋值），会把前面
        插件写的说明整段覆盖掉 —— 这种错只有同时配两段以上才暴露。
        """
        import mewcode.tools.plugins as P
        from mewcode.config import KnowledgeConfig

        monkeypatch.setattr(
            P, "discover_entry_points",
            lambda: [_FakeEP("acme", exc=ImportError("no acme sdk"))],
        )
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.plugins = True
        cfg.knowledge = KnowledgeConfig(kind="mock")
        cfg.environment = {"start": "bash up.sh"}
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        assert "acme" in asm.prompt_note          # 插件
        assert "知识库" in asm.prompt_note          # 知识库
        assert "环境管理" in asm.prompt_note        # 环境
        # 顺序：插件在最前（先装配）
        assert asm.prompt_note.index("acme") < asm.prompt_note.index("知识库")


# ---------------------------------------------------------------------------
# 五、校验
# ---------------------------------------------------------------------------


class TestValidate:
    def test_default_off(self):
        assert validate_toolset(None)["plugins"] is False
        assert validate_toolset({})["plugins"] is False

    def test_explicit_true(self):
        assert validate_toolset({"plugins": True})["plugins"] is True

    def test_must_be_bool(self):
        with pytest.raises(ConfigError, match="plugins"):
            validate_toolset({"plugins": "yes"})

    def test_flows_into_config(self, tmp_path):
        from mewcode.config import load_config

        import yaml

        cfg = {
            "providers": [{"name": "p", "protocol": "anthropic",
                           "base_url": "https://x", "model": "m", "api_key": "k"}],
            "toolset": {"plugins": True},
        }
        p = tmp_path / "c.yaml"
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        assert load_config(p).toolset.plugins is True

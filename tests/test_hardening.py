"""四个已知缺陷的修复验证。

    ① extract_summary 泄漏 <analysis>        → context/manager.py
    ② MCP 工具 category 恒为 command          → mcp/tool_wrapper.py + manager + config + validator
    ③ command 型 hook 通知到不了模型           → hooks/engine.py + models.py + loader.py + agent.py
    ④ RuleEngine 用 fnmatch，表达不了数值规则   → permissions/rules.py

① 的断言在 `test_extract_summary.py` 里（从 xfail 转正），这里覆盖 ②③④。
"""
from __future__ import annotations

import pytest

from mewcode.config import MCPServerConfig
from mewcode.hooks.engine import HookEngine
from mewcode.hooks.loader import HookConfigError, load_hooks
from mewcode.hooks.models import HookContext
from mewcode.permissions.rules import Rule, RuleEngine, parse_rule
from mewcode.validator import ConfigError, validate_mcp_servers


# ---------------------------------------------------------------------------
# ② MCP 工具 category 可配置
# ---------------------------------------------------------------------------


def _import_wrapper():
    """取 MCPToolWrapper。

    这里能直接 import，是因为 `mewcode/mcp/` 已经重命名为 `mewcode/mcpclient/`
    —— 之前它和第三方 `mcp` 包同名，pytest 把项目根加进 `sys.path` 后
    `from mcp import ...` 会解析到项目自己的包上，触发循环导入。
    """
    from mewcode.mcpclient.tool_wrapper import MCPToolWrapper

    return MCPToolWrapper


class _FakeDef:
    name = "t"
    description = "d"
    inputSchema: dict = {"properties": {}, "type": "object"}


def test_mcp_package_name_no_longer_collides():
    """把"包名冲突已解除"钉成一条测试，防止有人改回去。

    冲突的后果不只是 import 报错：`python -m mewcode`（cwd 在 sys.path 上）
    会直接起不来，因为 `mcpclient/client.py` 里的 `from mcp import ...`
    会解析到项目自己的同名包。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not (root / "mcp" / "__init__.py").exists(), (
        "mewcode/mcp 又出现了？它会遮蔽第三方 mcp 包"
    )
    assert (root / "mcpclient" / "__init__.py").exists()
    # 第三方 mcp 包必须能正常导入
    import mcp as third_party_mcp

    assert "site-packages" in (third_party_mcp.__file__ or ""), (
        f"`import mcp` 解析到了 {third_party_mcp.__file__}，不是第三方包"
    )


class TestMcpCategory:
    """旧实现无条件 `self.category = "command"`，对只读运维 MCP 有两个后果：
    既没有真正的护栏（只受 8 条 shell 正则约束），又每次查询都弹窗。
    """

    def test_validator_accepts_three_categories(self):
        for cat in ("read", "write", "command"):
            out = validate_mcp_servers([{"name": "x", "url": "http://a", "category": cat}])
            assert out[0]["category"] == cat

    def test_validator_defaults_to_command(self):
        """不写 category 时保持旧行为 —— 不能让已有配置悄悄变语义。"""
        out = validate_mcp_servers([{"name": "x", "url": "http://a"}])
        assert out[0]["category"] == "command"

    def test_validator_rejects_unknown_category(self):
        with pytest.raises(ConfigError):
            validate_mcp_servers([{"name": "x", "url": "http://a", "category": "hack"}])

    def test_config_passes_category_through(self, tmp_path):
        import yaml

        from mewcode.config import load_config

        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "providers": [
                        {"name": "p", "protocol": "anthropic", "base_url": "http://x",
                         "api_key": "k", "model": "m"}
                    ],
                    "mcp_servers": [
                        {"name": "loki", "url": "http://loki", "category": "read"}
                    ],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.mcp_servers[0].category == "read"

    def test_config_dataclass_default(self):
        assert MCPServerConfig(name="x").category == "command"

    def test_wrapper_normalizes_bad_category(self):
        """wrapper 自己也要兜底：传进来非法值就退回 command，不能变成无类别。"""
        MCPToolWrapper = _import_wrapper()
        for given, expected in [("read", "read"), ("write", "write"),
                                ("command", "command"), ("bogus", "command")]:
            w = MCPToolWrapper("s", _FakeDef(), object(), category=given)
            assert w.category == expected

    def test_readonly_mcp_tool_is_concurrency_safe(self):
        """read 类工具应该允许并发 —— 排查故障时会连发多个查询。"""
        MCPToolWrapper = _import_wrapper()
        assert MCPToolWrapper("s", _FakeDef(), object(), category="read").is_concurrency_safe
        assert not MCPToolWrapper("s", _FakeDef(), object(), category="write").is_concurrency_safe


# ---------------------------------------------------------------------------
# ③ hook 的两条通道分离
# ---------------------------------------------------------------------------


class TestHookNotificationChannels:
    """旧实现只有一个队列：`_drain_hook_events()`（送 UI）先把队列清空，
    于是 `drain_notifications()`（喂模型）永远拿到空 —— 同步 hook 的输出
    到不了模型。修法是把"喂模型"拆成独立队列 + 显式开关。
    """

    def _engine(self, notify_model: bool) -> HookEngine:
        hooks = load_hooks([
            {
                "id": "lint",
                "event": "post_tool_use",
                "action": {"type": "prompt", "message": "ruff 报了 3 个错"},
                "notify_model": notify_model,
            }
        ])
        return HookEngine(hooks)

    async def test_default_does_not_feed_model(self):
        """默认不进模型通道 —— 否则所有 hook 输出都会变成 system reminder，噪声很大。"""
        eng = self._engine(notify_model=False)
        await eng.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))
        assert len(eng.drain_notifications()) == 1     # UI 拿到
        assert eng.drain_model_reminders() == []        # 模型拿不到

    async def test_opt_in_feeds_model(self):
        eng = self._engine(notify_model=True)
        await eng.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))
        assert len(eng.drain_notifications()) == 1
        reminders = eng.drain_model_reminders()
        assert len(reminders) == 1
        assert "ruff 报了 3 个错" in reminders[0].output

    async def test_draining_ui_does_not_steal_model_channel(self):
        """★ 核心回归：把 UI 那条队列取空之后，模型那条**必须还在**。

        这正是旧实现坏掉的地方 —— 两条通道抢同一个 list。
        """
        eng = self._engine(notify_model=True)
        await eng.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))

        ui = eng.drain_notifications()          # 模拟 agent._drain_hook_events()
        assert len(ui) == 1
        model = eng.drain_model_reminders()     # 模拟 agent.py 里喂模型那一步
        assert len(model) == 1, "UI 通道把模型通道的队列偷走了"

    async def test_both_channels_are_drained_independently(self):
        eng = self._engine(notify_model=True)
        await eng.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))
        assert eng.drain_model_reminders()          # 先取模型那条
        assert len(eng.drain_notifications()) == 1  # UI 那条仍在

    def test_loader_reads_notify_model_flag(self):
        hooks = load_hooks([
            {"id": "a", "event": "turn_end", "action": {"type": "prompt", "message": "x"}},
            {"id": "b", "event": "turn_end", "action": {"type": "prompt", "message": "y"},
             "notify_model": True},
        ])
        assert hooks[0].notify_model is False
        assert hooks[1].notify_model is True

    async def test_empty_output_not_queued_for_model(self):
        """空输出不该生成一条空的 system reminder。

        绕过 loader 直接构造 Hook：loader 会拒绝空 message 的 prompt 动作，
        而这里要测的是**引擎侧的守卫**。
        """
        from mewcode.hooks.models import Action, Hook

        eng = HookEngine([
            Hook(id="empty", event="turn_end",
                 action=Action(type="prompt", message=""), notify_model=True)
        ])
        await eng.run_hooks("turn_end", HookContext(event_name="turn_end"))
        assert eng.drain_model_reminders() == []


# ---------------------------------------------------------------------------
# ④ 数值规则
# ---------------------------------------------------------------------------


class TestNumericRules:
    """`fnmatch` 只能做字符串通配，表达不了"金额超过多少"——
    而业务权限的核心恰恰常是阈值。现在支持 `Tool(field > 10000)` 语法。
    """

    @pytest.mark.parametrize("expr,content,expected", [
        ("CreatePO(amount > 10000)", "50000", True),
        ("CreatePO(amount > 10000)", "5000", False),
        ("CreatePO(amount > 10000)", "10000", False),
        ("CreatePO(amount >= 10000)", "10000", True),
        ("CreatePO(amount <= 10000)", "10000", True),
        ("CreatePO(amount < 100)", "99", True),
        ("CreatePO(amount == 42)", "42", True),
        ("CreatePO(amount != 42)", "43", True),
    ])
    def test_numeric_comparisons(self, expr, content, expected):
        rule = parse_rule(expr, "deny")
        assert rule.matches("CreatePO", content) is expected

    def test_non_numeric_content_does_not_match(self):
        """取不到数值时必须**不匹配**。

        如果这里降级成"匹配成功"，一个字段名写错的规则就会变成全量放行 ——
        那比不生效危险得多。
        """
        rule = parse_rule("CreatePO(amount > 10000)", "deny")
        assert rule.matches("CreatePO", "五万块") is False
        assert rule.matches("CreatePO", "") is False
        assert rule.matches("CreatePO", "N/A") is False

    def test_numeric_rule_is_tool_scoped(self):
        rule = parse_rule("CreatePO(amount > 10000)", "deny")
        assert rule.matches("OtherTool", "99999") is False

    def test_wildcard_rules_still_work(self):
        """数值语法不能破坏原有的通配语法。"""
        rule = parse_rule("Bash(pytest -q*)", "allow")
        assert rule.op == ""
        assert rule.matches("Bash", "pytest -q tests/") is True
        assert rule.matches("Bash", "rm -rf /") is False

    def test_numeric_expression_round_trips(self):
        """`pattern` 保存可回显的规范形式，写回 YAML 后还能解析回来。

        这条很关键：规则会被 `append_local_rule` 序列化成
        `f"{tool_name}({pattern})"` 写进 permissions.local.yaml。
        """
        rule = parse_rule("CreatePO(amount>10000)", "deny")
        assert rule.pattern == "amount > 10000"
        again = parse_rule(f"{rule.tool_name}({rule.pattern})", rule.effect)
        assert again.op == ">" and again.threshold == 10000.0

    def test_engine_uses_numeric_rule(self):
        """端到端：RuleEngine 真的能用数值规则拦下超额度操作。"""
        from pathlib import Path

        import yaml

        rules_file = Path(__file__).parent / ".tmp-num-rules.yaml"
        rules_file.write_text(
            yaml.safe_dump(
                [{"rule": "CreatePO(amount > 10000)", "effect": "deny"},
                 {"rule": "CreatePO(amount <= 10000)", "effect": "allow"}],
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        try:
            eng = RuleEngine(local_rules_path=rules_file)
            assert eng.evaluate("CreatePO", "50000") == "deny"
            assert eng.evaluate("CreatePO", "5000") == "allow"
            assert eng.evaluate("CreatePO", "没填金额") is None
        finally:
            rules_file.unlink(missing_ok=True)

    def test_invalid_numeric_syntax_falls_back_to_glob(self):
        """语法不完整（缺阈值）时退回通配规则，而不是抛异常。"""
        rule = parse_rule("CreatePO(amount >)", "deny")
        assert rule.op == ""
        assert rule.matches("CreatePO", "amount >") is True

    def test_dataclass_defaults_preserve_old_construction(self):
        """旧代码 `Rule(tool_name=..., pattern=..., effect=...)` 必须继续可用。"""
        r = Rule(tool_name="Bash", pattern="ls*", effect="allow")
        assert r.op == "" and r.threshold == 0.0
        assert r.matches("Bash", "ls -la") is True

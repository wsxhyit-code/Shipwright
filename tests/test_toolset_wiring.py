"""toolset 装配的测试。

这些测试针对的是一个**装配缺口**：`tools/ops/` 和 `tools/create_pr.py`
曾经是"测试全过但运行中的 agent 够不着"的代码 —— `app.py` 只挂内置工具，
运维工具和 CreatePR 一个都没挂。所以下面最重要的一条断言是：

    create_default_registry() 里没有 CreatePR，装配之后必须有。

    pytest tests/test_toolset_wiring.py -v
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from mewcode.config import ConfigError, ToolsetConfig, load_config
from mewcode.toolset import assemble_toolset
from mewcode.tools import create_default_registry
from mewcode.tools.ops.backend import MockOpsBackend, OpsError
from mewcode.tools.ops.backends import CompositeOpsBackend, LokiLogBackend
from mewcode.tools.ops.backends.factory import build_ops_backend
from mewcode.validator import validate_toolset

OPS_TOOL_NAMES = [
    "ListAlerts",
    "GetAlert",
    "QueryLogs",
    "GetLogSample",
    "QueryMetrics",
    "ListDeploys",
    "GetServiceHealth",
    "BuildTimeline",
    "CreateIncident",
]


# ---------------------------------------------------------------------------
# 一、核心断言：装配前没有，装配后有
# ---------------------------------------------------------------------------


class TestTheGapItself:
    def test_create_pr_is_not_in_the_default_registry(self):
        """先确认缺口真实存在 —— 内置注册表里确实没有 CreatePR。

        这条如果哪天失败了，说明有人把 CreatePR 挂进了 create_default_registry，
        那下面那些装配测试的意义就变了，需要重新想。
        """
        reg = create_default_registry()
        names = {t.name for t in reg.list_tools()}
        assert "CreatePR" not in names
        assert not (names & set(OPS_TOOL_NAMES))

    def test_assembly_actually_registers_create_pr(self, tmp_path):
        """装配之后，CreatePR 必须真的出现在注册表里。"""
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"

        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        assert "CreatePR" in {t.name for t in reg.list_tools()}
        assert "CreatePR" in assembly.tool_names

    def test_assembly_registers_all_ops_tools(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()

        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        names = {t.name for t in reg.list_tools()}
        for want in OPS_TOOL_NAMES:
            assert want in names, f"{want} 没被注册"
        assert set(assembly.tool_names) == set(OPS_TOOL_NAMES)

    def test_empty_toolset_changes_nothing(self, tmp_path):
        """没配 toolset 时行为必须和以前完全一样（默认不变）。"""
        before = create_default_registry()
        reg = create_default_registry()
        assembly = assemble_toolset(reg, ToolsetConfig(), agent=None, work_dir=tmp_path)

        assert {t.name for t in before.list_tools()} == {t.name for t in reg.list_tools()}
        assert assembly.tool_names == []
        assert assembly.prompt_note == ""


def _mock_ops_cfg():
    from mewcode.config import OpsBackendConfig

    return [OpsBackendConfig(kind="mock", capability="mock")]


# ---------------------------------------------------------------------------
# 二、能力清单要进 prompt（否则模型会把"没接入"当"没有异常"）
# ---------------------------------------------------------------------------


class TestPromptNote:
    def test_prompt_note_lists_missing_capabilities(self, tmp_path):
        from mewcode.config import OpsBackendConfig

        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = [OpsBackendConfig(kind="loki", capability="logs",
                                    base_url="http://loki.test")]

        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        assert assembly.prompt_note
        assert "日志" in assembly.prompt_note
        # 没接的能力必须列出来，并明确"这不代表没有问题"
        assert "部署" in assembly.prompt_note
        assert "不代表" in assembly.prompt_note

    def test_mock_backend_gives_no_capability_gaps(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()
        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        # MockOpsBackend 是个完整场景，八个方法都有
        assert "✗" not in assembly.prompt_note

    def test_mock_backend_still_produces_a_report(self, tmp_path):
        """★ "所有能力都接了" 和 "根本没生成报告" 必须可区分。

        `MockOpsBackend` 原来没有 `describe()`，于是 mock 场景下
        `ops_capability_report()` 返回空字符串 —— 调用方看到的是"没有 note"，
        分不清到底是"没有缺口"还是"报告没生成"。而"查不到 vs 没有"这种混淆
        在这个项目里已经出现过太多次（未接入 vs 没有异常、过期 ID vs 空结果）。
        """
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()
        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert assembly.prompt_note, "mock 后端也应该产出一份能力清单"
        assert "MockOpsBackend" in assembly.prompt_note

    def test_no_gap_warning_when_nothing_is_missing(self, tmp_path):
        """没有 ✗ 时不该附上"注意 ✗ 的能力"那条警告 —— 那会让模型怀疑一个完整的数据源。"""
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()
        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "标注 ✗" not in assembly.prompt_note

    def test_gap_warning_present_when_something_is_missing(self, tmp_path):
        from mewcode.config import OpsBackendConfig

        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = [OpsBackendConfig(kind="loki", capability="logs",
                                    base_url="http://loki.test")]
        assembly = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "标注 ✗" in assembly.prompt_note

    def test_mock_satisfies_the_describe_protocol(self):
        """`OpsBackend` 协议现在包含 describe()，mock 也要满足它。

        否则"换成真实后端"时会出现一个只在 mock 下才有的行为差异 ——
        而 mock 正是大家用来跑通流程的那个。
        """
        mock = MockOpsBackend()
        assert callable(getattr(mock, "describe", None))
        assert mock.describe().strip()
        assert callable(getattr(mock, "close", None))


# ---------------------------------------------------------------------------
# 三、配置校验
# ---------------------------------------------------------------------------


class TestValidateToolset:
    def test_defaults(self):
        out = validate_toolset(None)
        assert out["ops"] == []
        assert out["create_pr"]["enabled"] is False
        assert out["create_pr"]["require_independent"] is True
        # 默认交付模式是最保守的那一档
        assert out["create_pr"]["mode"] == "patch"

    # --- 交付模式 ---

    def test_unknown_mode_rejected(self):
        with pytest.raises(ConfigError, match="mode must be one of"):
            validate_toolset(
                {"create_pr": {"enabled": True, "verify_command": "pytest -q",
                               "mode": "yolo"}}
            )

    def test_known_modes_accepted(self, monkeypatch):
        import shutil

        # mode=pr 需要 gh 或 token 之一，否则会被另一条校验拦下
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
        for mode in ("patch", "push", "pr"):
            out = validate_toolset(
                {"create_pr": {"enabled": True, "verify_command": "pytest -q",
                               "mode": mode}}
            )
            assert out["create_pr"]["mode"] == mode

    def test_pr_mode_without_gh_or_token_is_rejected(self, monkeypatch):
        """★ mode=pr 但开不了 PR → 启动就报错。

        否则 agent 会在**改完代码、两层验证都通过、分支都推上去之后**
        才发现开不了 PR —— 白跑一整轮，而且分支已经到远端了。
        这种"跑到最后一米才失败"是最浪费的失败方式，所以提前到启动时挡。
        """
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        with pytest.raises(ConfigError) as exc:
            validate_toolset(
                {"create_pr": {"enabled": True, "verify_command": "pytest -q",
                               "mode": "pr"}}
            )
        msg = str(exc.value)
        assert "GITHUB_TOKEN" in msg
        assert "push" in msg  # 告诉用户可以退到 push 模式

    def test_pr_mode_with_token_is_accepted(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        out = validate_toolset(
            {"create_pr": {"enabled": True, "verify_command": "pytest -q",
                           "mode": "pr"}}
        )
        assert out["create_pr"]["mode"] == "pr"

    def test_push_mode_does_not_need_credentials(self, monkeypatch):
        """mode=push 只要能推就行，不需要能开 PR —— 不该被那条校验误伤。"""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        out = validate_toolset(
            {"create_pr": {"enabled": True, "verify_command": "pytest -q",
                           "mode": "push"}}
        )
        assert out["create_pr"]["mode"] == "push"

    def test_disabled_pr_mode_skips_credential_check(self, monkeypatch):
        """没开启时不该因为 mode=pr 就要求凭据。"""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        out = validate_toolset({"create_pr": {"mode": "pr"}})
        assert out["create_pr"]["enabled"] is False

    def test_remote_and_api_base_parsed(self):
        out = validate_toolset(
            {"create_pr": {"remote": "upstream", "api_base": "https://ghe.corp/api/v3"}}
        )
        assert out["create_pr"]["remote"] == "upstream"
        assert out["create_pr"]["api_base"] == "https://ghe.corp/api/v3"

    def test_non_string_remote_rejected(self):
        with pytest.raises(ConfigError, match="must be a string"):
            validate_toolset({"create_pr": {"remote": 123}})

    # --- 原有校验 ---

    def test_enabled_without_verify_command_is_rejected(self):
        """没有验证命令的 CreatePR 等于拆掉了那条铁律，必须在启动时拒掉。"""
        with pytest.raises(ConfigError, match="verify_command"):
            validate_toolset({"create_pr": {"enabled": True}})

    def test_enabled_with_verify_command_ok(self):
        out = validate_toolset(
            {"create_pr": {"enabled": True, "verify_command": "pytest -q"}}
        )
        assert out["create_pr"]["enabled"] is True
        assert out["create_pr"]["verify_command"] == "pytest -q"

    def test_unknown_kind_rejected(self):
        with pytest.raises(ConfigError, match="unknown kind"):
            validate_toolset({"ops": [{"kind": "datadog", "base_url": "http://x"}]})

    def test_bad_capability_rejected(self):
        with pytest.raises(ConfigError, match="unknown capability"):
            validate_toolset(
                {"ops": [{"kind": "loki", "base_url": "http://x", "capability": "nope"}]}
            )

    def test_duplicate_capability_rejected(self):
        """同一能力配两个后端是无意义的 —— 路由是一对一。"""
        with pytest.raises(ConfigError, match="已经配过"):
            validate_toolset(
                {
                    "ops": [
                        {"kind": "loki", "base_url": "http://a"},
                        {"kind": "loki", "base_url": "http://b"},
                    ]
                }
            )

    def test_kind_defaults_capability(self):
        out = validate_toolset(
            {
                "ops": [
                    {"kind": "loki", "base_url": "http://l"},
                    {"kind": "prometheus", "base_url": "http://p"},
                    {"kind": "alertmanager", "base_url": "http://a"},
                ]
            }
        )
        assert [o["capability"] for o in out["ops"]] == ["logs", "metrics", "alerts"]

    def test_base_url_required_for_real_backends(self):
        with pytest.raises(ConfigError, match="需要 base_url"):
            validate_toolset({"ops": [{"kind": "loki"}]})

    def test_mock_must_not_have_base_url(self):
        with pytest.raises(ConfigError, match="不需要 base_url"):
            validate_toolset({"ops": [{"kind": "mock", "base_url": "http://x"}]})

    def test_ops_must_be_a_list(self):
        with pytest.raises(ConfigError, match="must be a list"):
            validate_toolset({"ops": {"kind": "loki"}})

    def test_bad_timeout_rejected(self):
        with pytest.raises(ConfigError, match="timeout"):
            validate_toolset(
                {"ops": [{"kind": "loki", "base_url": "http://x", "timeout": 0}]}
            )


# ---------------------------------------------------------------------------
# 四、后端工厂
# ---------------------------------------------------------------------------


class TestBuildOpsBackend:
    def test_none_when_no_specs(self):
        assert build_ops_backend([]) is None
        assert build_ops_backend(None) is None

    def test_mock_returns_mock(self):
        backend = build_ops_backend([{"kind": "mock", "capability": "mock"}])
        assert isinstance(backend, MockOpsBackend)

    def test_mock_cannot_be_mixed(self):
        with pytest.raises(OpsError, match="不能和其它后端混用"):
            build_ops_backend(
                [
                    {"kind": "mock", "capability": "mock"},
                    {"kind": "loki", "capability": "logs", "base_url": "http://l"},
                ]
            )

    def test_builds_composite_routing(self):
        backend = build_ops_backend(
            [
                {"kind": "loki", "capability": "logs", "base_url": "http://l"},
                {"kind": "prometheus", "capability": "metrics", "base_url": "http://p"},
            ]
        )
        assert isinstance(backend, CompositeOpsBackend)
        assert set(backend.wired()) == {"logs", "metrics"}

    def test_token_read_from_env(self):
        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l",
              "token_env": "TEST_LOKI_TOKEN"}],
            env={"TEST_LOKI_TOKEN": "s3cret"},
        )
        # 走私有属性确认 token 真的被读进去了（这正是要防的静默失败点）
        assert isinstance(backend, CompositeOpsBackend)
        assert backend._providers["logs"]._client.headers["Authorization"] == "Bearer s3cret"

    def test_missing_token_env_fails_loudly(self):
        """环境变量没读到必须报错，不能静默发一个无认证请求。

        静默发出去的结果是一个 401，而 401 在排查时很容易被误读成
        "服务端权限配错了"，而不是"我的 token 根本没读到"。
        """
        with pytest.raises(OpsError, match="TEST_MISSING_TOKEN"):
            build_ops_backend(
                [{"kind": "loki", "capability": "logs", "base_url": "http://l",
                  "token_env": "TEST_MISSING_TOKEN"}],
                env={},
            )

    def test_empty_token_env_value_also_fails(self):
        with pytest.raises(OpsError, match="TEST_EMPTY_TOKEN"):
            build_ops_backend(
                [{"kind": "loki", "capability": "logs", "base_url": "http://l",
                  "token_env": "TEST_EMPTY_TOKEN"}],
                env={"TEST_EMPTY_TOKEN": "   "},
            )

    def test_no_token_env_means_anonymous(self):
        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l"}],
            env={},
        )
        assert "Authorization" not in backend._providers["logs"]._client.headers

    def test_unknown_kind_rejected(self):
        with pytest.raises(OpsError, match="不认识的后端类型"):
            build_ops_backend([{"kind": "datadog", "capability": "metrics",
                                "base_url": "http://d"}])

    def test_unwired_capability_still_raises_at_call_time(self):
        """只接了日志时，部署能力要报错 —— 不是返回空。"""
        from mewcode.tools.ops.backends import OpsCapabilityMissing

        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l"}]
        )
        with pytest.raises(OpsCapabilityMissing, match="部署"):
            backend.list_deploys("orders-api")


# ---------------------------------------------------------------------------
# 五、配置加载：环境变量缺失在启动时就炸
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _base_config(**extra) -> dict:
    cfg = {
        "providers": [
            {
                "name": "test",
                "protocol": "anthropic",
                "base_url": "https://api.example.com",
                "model": "test-model",
                "api_key": "sk-test",
            }
        ]
    }
    cfg.update(extra)
    return cfg


class TestLoadConfigToolset:
    def test_toolset_loaded_from_config(self, tmp_path):
        p = _write_config(
            tmp_path,
            _base_config(
                toolset={
                    "ops": [{"kind": "mock", "capability": "mock"}],
                    "create_pr": {"enabled": True, "verify_command": "pytest -q"},
                }
            ),
        )
        cfg = load_config(p)
        assert len(cfg.toolset.ops) == 1
        assert cfg.toolset.create_pr.enabled is True
        assert cfg.toolset.create_pr.verify_command == "pytest -q"

    def test_missing_toolset_uses_defaults(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, _base_config()))
        assert cfg.toolset.ops == []
        assert cfg.toolset.create_pr.enabled is False

    def test_missing_token_env_fails_at_config_load(self, tmp_path, monkeypatch):
        """这一条是这次的重点：问题在**启动时**暴露，而不是半夜排查时。"""
        monkeypatch.delenv("TEST_ABSENT_TOKEN", raising=False)
        p = _write_config(
            tmp_path,
            _base_config(
                toolset={
                    "ops": [
                        {"kind": "loki", "capability": "logs",
                         "base_url": "http://l", "token_env": "TEST_ABSENT_TOKEN"}
                    ]
                }
            ),
        )
        with pytest.raises(ConfigError) as exc:
            load_config(p)
        msg = str(exc.value)
        assert "TEST_ABSENT_TOKEN" in msg
        assert "凭据缺失" in msg

    def test_present_token_env_passes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_PRESENT_TOKEN", "abc")
        p = _write_config(
            tmp_path,
            _base_config(
                toolset={
                    "ops": [
                        {"kind": "loki", "capability": "logs",
                         "base_url": "http://l", "token_env": "TEST_PRESENT_TOKEN"}
                    ]
                }
            ),
        )
        cfg = load_config(p)
        assert cfg.toolset.ops[0].token_env == "TEST_PRESENT_TOKEN"


# ---------------------------------------------------------------------------
# 六、config.local.yaml 覆盖层
# ---------------------------------------------------------------------------


class TestToolsetMerge:
    def test_ops_merge_by_capability(self):
        from mewcode.config import (AppConfig, OpsBackendConfig, ProviderConfig,
                                    _merge_config)
        from mewcode.validator import DEFAULT_CREATE_PR

        base = AppConfig(providers=[ProviderConfig("a", "anthropic", "u", "m", "k")])
        base.toolset.ops = [OpsBackendConfig(kind="loki", capability="logs",
                                             base_url="http://prod-loki")]

        override = AppConfig(providers=[])
        override.toolset.ops = [
            OpsBackendConfig(kind="loki", capability="logs", base_url="http://local-loki"),
            OpsBackendConfig(kind="prometheus", capability="metrics",
                             base_url="http://local-prom"),
        ]

        merged = _merge_config(base, override)
        by_cap = {o.capability: o.base_url for o in merged.toolset.ops}
        assert by_cap["logs"] == "http://local-loki"   # 同能力被顶掉
        assert by_cap["metrics"] == "http://local-prom"  # 新能力被追加
        assert len(merged.toolset.ops) == 2
        assert DEFAULT_CREATE_PR  # 只是引用一下，确认常量还在

    def test_create_pr_replaced_wholesale(self):
        from mewcode.config import (AppConfig, CreatePRConfig, ProviderConfig,
                                    _merge_config)

        base = AppConfig(providers=[ProviderConfig("a", "anthropic", "u", "m", "k")])
        override = AppConfig(providers=[])
        override.toolset.create_pr = CreatePRConfig(
            enabled=True, verify_command="make check", base_ref="develop"
        )

        merged = _merge_config(base, override)
        assert merged.toolset.create_pr.enabled is True
        assert merged.toolset.create_pr.verify_command == "make check"
        assert merged.toolset.create_pr.base_ref == "develop"

    def test_empty_override_keeps_base(self):
        from mewcode.config import AppConfig, OpsBackendConfig, ProviderConfig, _merge_config

        base = AppConfig(providers=[ProviderConfig("a", "anthropic", "u", "m", "k")])
        base.toolset.ops = [OpsBackendConfig(kind="loki", capability="logs",
                                             base_url="http://base-loki")]
        merged = _merge_config(base, AppConfig(providers=[]))
        assert merged.toolset.ops[0].base_url == "http://base-loki"


# ---------------------------------------------------------------------------
# 七、CreatePR 的验证器：装配时必须真的接上独立验证者
# ---------------------------------------------------------------------------


class TestCreatePRWiring:
    def test_verifier_runner_attached_when_agent_present(self, tmp_path):
        """require_independent=True 时，装配必须真的造出 verifier_runner。

        否则 CreatePR 会在运行时才发现"没配独立验证"，
        而配置看起来是完全正确的。
        """
        reg = create_default_registry()

        class _FakeAgent:
            registry = reg
            client = None
            protocol = "anthropic"
            work_dir = str(tmp_path)

        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"

        assemble_toolset(reg, cfg, agent=_FakeAgent(), work_dir=tmp_path)

        tool = next(t for t in reg.list_tools() if t.name == "CreatePR")
        assert tool._verifier_runner is not None
        assert tool._require_independent is True

    def test_no_verifier_runner_without_agent(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"

        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        tool = next(t for t in reg.list_tools() if t.name == "CreatePR")
        assert tool._verifier_runner is None

    def test_relative_artifacts_dir_resolves_against_work_dir(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"
        cfg.create_pr.artifacts_dir = "out/pr"

        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        tool = next(t for t in reg.list_tools() if t.name == "CreatePR")
        assert tool._artifacts == tmp_path / "out" / "pr"

    def test_absolute_artifacts_dir_kept(self, tmp_path):
        reg = create_default_registry()
        target = tmp_path / "abs-pr"
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"
        cfg.create_pr.artifacts_dir = str(target)

        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        tool = next(t for t in reg.list_tools() if t.name == "CreatePR")
        assert tool._artifacts == target


# ---------------------------------------------------------------------------
# 八、权限语义：运维读工具不该弹窗，建单该弹窗
# ---------------------------------------------------------------------------


class TestPermissionSemantics:
    def test_ops_read_tools_are_read_category(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()
        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        by_name = {t.name: t for t in reg.list_tools()}
        for name in ("ListAlerts", "GetAlert", "QueryLogs", "GetLogSample",
                     "QueryMetrics", "ListDeploys", "GetServiceHealth"):
            assert by_name[name].category == "read", name

    def test_create_incident_is_write(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = _mock_ops_cfg()
        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        by_name = {t.name: t for t in reg.list_tools()}
        assert by_name["CreateIncident"].category == "write"

    def test_create_pr_is_command_category(self, tmp_path):
        """CreatePR 有外部副作用（补丁会被 CI 用于推送），不能算 read。"""
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"
        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        tool = next(t for t in reg.list_tools() if t.name == "CreatePR")
        assert tool.category == "command"
        assert tool.is_concurrency_safe is False

    def test_readonly_registry_excludes_create_pr(self, tmp_path):
        """独立验证者的注册表里绝不能有 CreatePR —— 否则它会递归提 PR。"""
        from mewcode.tools.agent_verify import build_readonly_registry

        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.create_pr.enabled = True
        cfg.create_pr.verify_command = "python -c pass"
        cfg.ops = _mock_ops_cfg()
        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        ro = build_readonly_registry(reg)
        ro_names = {t.name for t in ro.list_tools()}
        assert "CreatePR" not in ro_names
        assert "CreateIncident" not in ro_names  # 写操作同样不在
        assert "ReadFile" in ro_names


# ---------------------------------------------------------------------------
# 九、环境变量名写错的项目级检查（走真实 candidate 列表）
# ---------------------------------------------------------------------------


class TestProjectConfigDiscovery:
    def test_project_config_with_bad_token_env_fails(self, tmp_path, monkeypatch):
        """模拟真实启动：cwd 下有 .mewcode/config.yaml，token_env 读不到。"""
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        d = tmp_path / ".mewcode"
        d.mkdir()
        (d / "config.yaml").write_text(
            yaml.safe_dump(
                _base_config(
                    toolset={
                        "ops": [
                            {"kind": "prometheus", "capability": "metrics",
                             "base_url": "http://p", "token_env": "NOPE_TOKEN"}
                        ]
                    }
                ),
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

        with pytest.raises(ConfigError, match="NOPE_TOKEN"):
            load_config()

    def test_env_at_build_time_is_used(self, monkeypatch):
        """确认 factory 真的读了 os.environ（而不是只读传入的 env）。"""
        monkeypatch.setenv("REAL_ENV_TOKEN", "from-env")
        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l",
              "token_env": "REAL_ENV_TOKEN"}]
        )
        assert (
            backend._providers["logs"]._client.headers["Authorization"]
            == "Bearer from-env"
        )
        assert os.environ["REAL_ENV_TOKEN"] == "from-env"


# ---------------------------------------------------------------------------
# 十、和真实后端类对得上
# ---------------------------------------------------------------------------


class TestFactoryMatchesBackendClasses:
    def test_loki_kind_builds_loki_class(self):
        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l"}]
        )
        assert isinstance(backend._providers["logs"], LokiLogBackend)

    def test_close_propagates_through_factory(self):
        """factory 造出来的东西必须能被正常关闭，不漏 socket。"""
        backend = build_ops_backend(
            [{"kind": "loki", "capability": "logs", "base_url": "http://l"}]
        )
        backend.close()  # 不抛异常即通过

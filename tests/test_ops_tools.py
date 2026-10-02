"""运维工具测试。

验证三件事：
  1. 权限映射正确：查询 → allow（不该弹窗）、建单 → ask（必须确认）
  2. **日志返回的是聚类不是全量**（这是不把上下文撑爆的关键设计）
  3. 建故障工单**强制要证据链**（没有依据的根因要被拒）
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
from mewcode.tools import create_default_registry
from mewcode.tools.ops import MockOpsBackend, OPS_TOOLS, register_ops_tools


@pytest.fixture
def backend() -> MockOpsBackend:
    return MockOpsBackend()


@pytest.fixture
def registry(backend):
    reg = create_default_registry()
    register_ops_tools(reg, backend)
    return reg


@pytest.fixture
def checker(tmp_path):
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )


async def run(registry, name, **args):
    tool = registry.get(name)
    assert tool is not None, f"{name} 未注册"
    return await tool.execute(tool.params_model.model_validate(args))


# ---------------------------------------------------------------------------
# 权限映射
# ---------------------------------------------------------------------------


class TestPermissionMapping:
    def test_all_ops_tools_registered(self, registry):
        names = {t.name for t in registry.list_tools()}
        for cls in OPS_TOOLS:
            assert cls.name in names

    def test_only_create_incident_is_a_write(self):
        writes = [c.name for c in OPS_TOOLS if c.category == "write"]
        assert writes == ["CreateIncident"], "只有建单该是写操作，其余必须只读"

    def test_reads_are_allowed_without_prompt(self, registry, checker):
        """运维排查要连续调很多次查询，每次都弹窗就没法用了。"""
        cases = [
            ("ListAlerts", {}),
            ("QueryLogs", {"service": "orders-api"}),
            ("QueryMetrics", {"metric": "http_5xx_rate"}),
            ("ListDeploys", {}),
            ("BuildTimeline", {"service": "orders-api"}),
        ]
        for name, args in cases:
            d = checker.check(registry.get(name), args)
            assert d.effect == "allow", f"{name} 在 DEFAULT 下弹窗了: {d.effect}"

    def test_create_incident_requires_confirmation(self, registry, checker):
        d = checker.check(
            registry.get("CreateIncident"),
            {"service": "orders-api", "title": "t", "severity": "critical",
             "root_cause": "r", "evidence": "e"},
        )
        assert d.effect == "ask"

    def test_no_dangerous_ops_tools(self):
        """刻意不提供回滚/重启这类工具 —— 那些该走 CI 流水线。"""
        names = {c.name for c in OPS_TOOLS}
        for forbidden in ("Rollback", "RestartService", "ScaleUp", "DeletePod"):
            assert forbidden not in names


# ---------------------------------------------------------------------------
# 日志：聚类而不是全量
# ---------------------------------------------------------------------------


class TestLogClustering:
    async def test_query_logs_returns_clusters_not_raw(self, registry):
        r = await run(registry, "QueryLogs", service="orders-api")
        assert not r.is_error
        # 有聚类信息
        assert "C-1" in r.output and "1,204 条" in r.output
        assert "首次 10:23:41" in r.output
        # 不是原始终端日志：整段输出必须很短（否则就是撑爆上下文的写法）
        assert len(r.output) < 1500, f"聚类输出过长（{len(r.output)} 字符），可能返回了原始日志"
        # 只给一行样例，不是完整堆栈
        assert r.output.count("NullPointerException") <= 2

    async def test_query_logs_points_to_next_steps(self, registry):
        """工具描述里的工作流要体现在输出里，模型才知道下一步做什么。"""
        r = await run(registry, "QueryLogs", service="orders-api")
        assert "GetLogSample" in r.output
        assert "ListDeploys" in r.output

    async def test_get_log_sample_is_bounded(self, registry):
        r = await run(registry, "GetLogSample", cluster_id="C-1", count=4)
        assert not r.is_error
        assert "OrderService.java:142" in r.output

    async def test_sample_count_actually_limits(self, registry, backend):
        """count 必须真的生效 —— 这是防止把大段日志灌进上下文的闸门。"""
        one = await run(registry, "GetLogSample", cluster_id="C-1", count=1)
        four = await run(registry, "GetLogSample", cluster_id="C-1", count=4)
        assert len(one.output.splitlines()) < len(four.output.splitlines())

    async def test_log_sample_rejects_unknown_cluster(self, registry):
        r = await run(registry, "GetLogSample", cluster_id="C-NOPE")
        assert r.is_error and "不存在" in r.output

    async def test_log_sample_count_has_upper_bound(self, registry):
        """上限是 schema 层面的：模型没法一次拉 10000 行。"""
        tool = registry.get("GetLogSample")
        with pytest.raises(Exception):
            tool.params_model.model_validate({"cluster_id": "C-1", "count": 9999})

    async def test_empty_window_is_handled(self, registry):
        r = await run(registry, "QueryLogs", service="orders-api", pattern="绝不匹配的关键字")
        assert not r.is_error and "没有" in r.output


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


class TestMetrics:
    async def test_metrics_compare_against_baseline(self, registry):
        r = await run(registry, "QueryMetrics", metric="http_5xx_rate")
        assert not r.is_error
        assert "pod-3" in r.output
        # 要给出"哪个分组异常"的提示，而不是丢一堆数字
        assert "异常分组" in r.output and "pod-3" in r.output

    async def test_baseline_is_window_start_not_median_of_head(self, registry, backend):
        """回归：基线曾是"前 3 点的中位数"，而尖峰从第 2 点就开始了，
        结果把尖峰算进基线 → 异常检测失效。必须取窗口起始值。"""
        s = backend.query_metrics("http_5xx_rate", "30m", "pod")[2]  # pod-3
        assert s.baseline == 0.1, f"基线应为窗口起始值 0.1，实得 {s.baseline}"
        assert s.latest == 12.3
        assert s.is_anomalous()

    async def test_small_relative_change_is_not_flagged(self, registry, backend):
        """0.1% → 0.2% 是 2 倍，但完全没意义，不该报异常。"""
        pod1 = backend.query_metrics("http_5xx_rate", "30m", "pod")[0]
        assert pod1.latest == 0.2
        assert not pod1.is_anomalous(), "噪声被当成故障了"

    async def test_metrics_unknown_name_lists_available(self, registry):
        r = await run(registry, "QueryMetrics", metric="不存在.的指标")
        assert r.is_error and "可用" in r.output


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------


class TestTimeline:
    async def test_timeline_orders_events(self, registry):
        r = await run(registry, "BuildTimeline", service="orders-api")
        assert not r.is_error
        out = r.output
        # 部署在 10:11，日志首现在 10:23 —— 顺序必须体现出来
        assert "10:11" in out and "10:23" in out
        assert out.index("10:11") < out.index("[日志首现]")

    async def test_timeline_does_not_draw_conclusions(self, registry):
        """工具只摆事实，结论交给模型 —— 否则就是把判断也机械化了。"""
        r = await run(registry, "BuildTimeline", service="orders-api")
        assert "因果关系需要你" in r.output

    async def test_timeline_unknown_service_is_empty_not_error(self, registry):
        r = await run(registry, "BuildTimeline", service="no-such-service")
        assert not r.is_error


# ---------------------------------------------------------------------------
# 故障工单：强制证据链
# ---------------------------------------------------------------------------


class TestIncident:
    async def test_requires_evidence(self, registry):
        """★ 没有证据链的根因等于猜测，必须被拒。"""
        r = await run(
            registry, "CreateIncident",
            service="orders-api", title="5xx 突增", severity="critical",
            root_cause="代码有问题", evidence="   ",
        )
        assert r.is_error and "证据链" in r.output

    async def test_valid_severity_enforced(self, registry):
        r = await run(
            registry, "CreateIncident",
            service="orders-api", title="t", severity="P0",
            root_cause="r", evidence="e",
        )
        assert r.is_error and "严重度" in r.output

    async def test_empty_title_rejected(self, registry):
        r = await run(
            registry, "CreateIncident",
            service="orders-api", title="  ", severity="critical",
            root_cause="r", evidence="e",
        )
        assert r.is_error and "标题" in r.output

    async def test_successful_creation(self, registry):
        r = await run(
            registry, "CreateIncident",
            service="orders-api", title="orders-api 5xx 突增", severity="critical",
            root_cause="v2.14.3 删掉了 getProfile() 的判空",
            evidence="日志聚类 C-1（OrderService.java:142 NPE，1204 条）；"
                     "指标 http_5xx_rate 仅 pod-3 从 0.1% 升到 12.3%；"
                     "部署 D-5521 v2.14.3 于 10:11 发布到 pod-3",
        )
        assert not r.is_error
        assert "INC-0001" in r.output


# ---------------------------------------------------------------------------
# 端到端：一次完整的故障定位链路（不用 LLM，只验证工具能串起来）
# ---------------------------------------------------------------------------


class TestIncidentWorkflow:
    async def test_full_triage_chain(self, registry):
        """按 skill 里的 SOP 走一遍，验证每一步都能拿到下一步需要的线索。"""
        # 1. 看告警
        alerts = await run(registry, "ListAlerts")
        assert "AL-7781" in alerts.output

        # 2. 告警详情
        detail = await run(registry, "GetAlert", alert_id="AL-7781")
        assert "orders-api" in detail.output

        # 3. 日志聚类 → 拿到主要错误
        logs = await run(registry, "QueryLogs", service="orders-api")
        assert "OrderService.java:142" in logs.output

        # 4. 指标 → 发现只有 pod-3 异常
        metrics = await run(registry, "QueryMetrics", metric="http_5xx_rate")
        assert "pod-3" in metrics.output and "异常分组" in metrics.output

        # 5. 部署 → 拿到 pod-3 上的最近发布 + commit
        deploys = await run(registry, "ListDeploys", service="orders-api")
        assert "v2.14.3" in deploys.output and "9b2c1f4" in deploys.output
        assert "pod-3" in deploys.output

        # 6. 时间线 → 把上面几条对齐
        timeline = await run(registry, "BuildTimeline", service="orders-api")
        assert "[部署]" in timeline.output and "[日志首现]" in timeline.output

        # 7. 建单（这一步在真实流程里需要人工确认）
        inc = await run(
            registry, "CreateIncident",
            service="orders-api", title="orders-api 5xx 突增", severity="critical",
            root_cause="见证据链",
            evidence="C-1 / pod-3 / D-5521 v2.14.3 / commit 9b2c1f4",
        )
        assert not inc.is_error

    async def test_deploy_gives_commit_for_code_lookup(self, registry):
        """部署记录必须带 commit —— 否则 agent 无法回到代码里定位。"""
        r = await run(registry, "ListDeploys", service="orders-api", limit=1)
        assert "commit=9b2c1f4" in r.output

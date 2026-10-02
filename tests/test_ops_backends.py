"""真实运维后端的测试。

用 `httpx.MockTransport` 拦截 HTTP —— 不需要起 Loki / Prometheus / Alertmanager，
但**走的是真实的请求构造和响应解析**，所以测出来的是真东西。

    pytest tests/test_ops_backends.py -v
"""
from __future__ import annotations

import json

import httpx
import pytest

from mewcode.tools.ops.backend import OpsError
from mewcode.tools.ops.backends import (
    AlertmanagerBackend,
    CompositeOpsBackend,
    LokiLogBackend,
    OpsCapabilityMissing,
    PrometheusMetricBackend,
    log_signature,
    parse_window,
    window_bounds,
)


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------


class _Recorder:
    """记录后端发出的请求，供断言检查。"""

    def __init__(self, payload, status: int = 200, raw: str | None = None):
        self.payload = payload
        self.status = status
        self.raw = raw
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raw is not None:
            return httpx.Response(self.status, text=self.raw)
        return httpx.Response(self.status, json=self.payload)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "后端没有发出任何请求"
        return self.requests[-1]

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def make_client(recorder: _Recorder) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(recorder),
        base_url="http://ops.internal",
        headers={"Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# 一、日志签名归一化 —— 聚类能不能用的根子在这里
# ---------------------------------------------------------------------------


class TestLogSignature:
    def test_strips_timestamp_and_level(self):
        sig = log_signature("2026-09-29 10:23:41.203 ERROR something failed")
        assert "2026-09-29" not in sig
        assert "10:23:41" not in sig
        assert "<L>" in sig
        assert "something failed" in sig

    def test_same_error_with_different_ids_collapses(self):
        """这是聚类的意义：1,204 条日志要能塌成 1 类。"""
        a = log_signature("2026-09-29 10:23:41 ERROR req 12345 failed for user 99")
        b = log_signature("2026-09-29 10:38:02 ERROR req 98765 failed for user 12")
        assert a == b

    def test_uuid_ip_hex_path_are_normalised(self):
        sig = log_signature(
            "ERROR connecting to 10.0.0.7:8080 id "
            "550e8400-e29b-41d4-a716-446655440000 at /var/log/app/x.log"
        )
        assert "<IP>" in sig
        assert "<UUID>" in sig
        assert "<PATH>" in sig

    def test_ip_not_shattered_into_numbers(self):
        """顺序坑：数字规则如果跑在 IP 前面，10.0.0.7 会被打碎。"""
        assert "<IP>" in log_signature("dial 10.0.0.7 refused")

    def test_different_exceptions_stay_separate(self):
        a = log_signature("ERROR java.lang.NullPointerException here")
        b = log_signature("ERROR java.util.concurrent.TimeoutException here")
        assert a != b

    @pytest.mark.parametrize(
        "a,b",
        [
            # 单位紧跟数字 —— 真实日志里最常见，也是 `\b` 会漏掉的一类
            ("WARN charge timeout after 3000ms", "WARN charge timeout after 3001ms"),
            ("WARN took 15s to respond", "WARN took 3s to respond"),
            ("INFO freed 1.5GB", "INFO freed 2.25GB"),
            ("ERROR sent 1204req", "ERROR sent 87req"),
            ("WARN disk at 91% full", "WARN disk at 73% full"),
            # 版本号：不能被拆成只替换第一段
            ("ERROR on api v2.14.3", "ERROR on api v2.15.0"),
            ("ERROR upgrade 1.2.3.4 failed", "ERROR upgrade 9.8.7.6 failed"),
            # 十六进制 trace id
            ("ERROR trace=00a1b2c3 done", "ERROR trace=ff99ee77 done"),
        ],
    )
    def test_values_that_vary_still_collapse(self, a, b):
        """这些是聚类的成败所在：任何一类没归一化，聚类数就会爆炸。

        特别盯 `3000ms` —— `\\b\\d+\\b` 在这里匹配不上（`0` 和 `m` 之间
        没有词边界），曾经导致 37 条超时日志裂成 37 个聚类。
        """
        assert log_signature(a) == log_signature(b), (
            f"没归一化：{log_signature(a)!r} != {log_signature(b)!r}"
        )

    def test_unit_suffix_is_preserved(self):
        """数字被换掉，但单位要留着 —— 否则 `3000ms` 和 `3000MB` 会混成一类。"""
        assert "<N>ms" in log_signature("WARN charge timeout after 3000ms")
        assert "<N>GB" in log_signature("INFO freed 1.5GB")
        assert log_signature("timeout 3000ms") != log_signature("freed 3000MB")

    def test_version_not_partially_replaced(self):
        sig = log_signature("deployed v2.14.3")
        assert "<VER>" in sig
        assert "14" not in sig and "3" not in sig

    def test_empty_and_whitespace(self):
        assert log_signature("") == ""
        assert log_signature("   \n\t ") == ""

    def test_truncates_long_lines(self):
        assert len(log_signature("ERROR " + "x" * 500)) <= 140


# ---------------------------------------------------------------------------
# 二、时间窗口
# ---------------------------------------------------------------------------


class TestWindow:
    @pytest.mark.parametrize(
        "text,seconds", [("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400)]
    )
    def test_parse(self, text, seconds):
        assert parse_window(text).total_seconds() == seconds

    def test_bad_window_raises_readable_error(self):
        with pytest.raises(OpsError, match="无法解析时间窗口"):
            parse_window("15 minutes")

    def test_bounds_are_ordered(self):
        start, end = window_bounds("15m")
        assert end > start
        assert (end - start).total_seconds() == pytest.approx(900, abs=1)


# ---------------------------------------------------------------------------
# 三、Loki
# ---------------------------------------------------------------------------

LOKI_RESPONSE = {
    "status": "success",
    "data": {
        "result": [
            {
                "stream": {"service": "orders-api", "level": "ERROR"},
                "values": [
                    ["1759131821000000000", "2026-09-29 10:23:41 ERROR req 1 failed"],
                    ["1759131822000000000", "2026-09-29 10:23:42 ERROR req 2 failed"],
                    ["1759131823000000000", "2026-09-29 10:23:43 ERROR req 3 failed"],
                    ["1759131824000000000", "2026-09-29 10:23:44 ERROR req 4 failed"],
                    ["1759131825000000000", "2026-09-29 10:23:45 ERROR req 5 failed"],
                ],
            },
            {
                "stream": {"service": "orders-api"},
                "values": [
                    ["1759131902000000000", "2026-09-29 10:25:02 WARN disk usage high 91%"],
                ],
            },
        ]
    },
}


class TestLoki:
    def _backend(self, recorder):
        return LokiLogBackend("http://ops.internal", client=make_client(recorder))

    def test_builds_logql_and_time_range(self):
        rec = _Recorder(LOKI_RESPONSE)
        self._backend(rec).query_logs("orders-api", "15m")
        params = rec.params()
        assert rec.last.url.path == "/loki/api/v1/query_range"
        assert 'service="orders-api"' in params["query"]
        assert "level=~" in params["query"]
        # 纳秒时间戳，且 end > start
        assert int(params["end"]) > int(params["start"])
        assert params["direction"] == "backward"

    def test_pattern_becomes_line_filter(self):
        rec = _Recorder(LOKI_RESPONSE)
        self._backend(rec).query_logs("orders-api", "15m", pattern="NullPointer")
        assert "|~" in rec.params()["query"]
        assert "NullPointer" in rec.params()["query"]

    def test_clusters_by_signature_sorted_by_count(self):
        rec = _Recorder(LOKI_RESPONSE)
        clusters = self._backend(rec).query_logs("orders-api", "15m")
        assert len(clusters) == 2
        assert clusters[0].count == 5
        assert clusters[1].count == 1
        assert clusters[0].services == ["orders-api"]
        # ID 由签名派生（不是位置序号），格式是 C-<6 位十六进制>
        assert clusters[0].cluster_id.startswith("C-")
        assert len(clusters[0].cluster_id) == 8
        assert clusters[0].cluster_id != clusters[1].cluster_id

    def test_sample_is_capped(self):
        """样例必须封顶 —— 不封顶就等于把原始日志塞进上下文。"""
        rec = _Recorder(LOKI_RESPONSE)
        clusters = self._backend(rec).query_logs("orders-api", "15m")
        assert len(clusters[0].sample) == 3
        assert clusters[0].count == 5  # 条数仍然是全量

    def test_time_range_is_reported(self):
        rec = _Recorder(LOKI_RESPONSE)
        top = self._backend(rec).query_logs("orders-api", "15m")[0]
        assert top.first_seen and top.last_seen
        assert top.first_seen < top.last_seen

    def test_get_log_sample_after_query(self):
        rec = _Recorder(LOKI_RESPONSE)
        backend = self._backend(rec)
        clusters = backend.query_logs("orders-api", "15m")
        assert len(backend.get_log_sample(clusters[0].cluster_id, 2)) == 2

    def test_get_log_sample_without_query_is_clear_error(self):
        rec = _Recorder(LOKI_RESPONSE)
        with pytest.raises(OpsError, match="不在最近一次"):
            self._backend(rec).get_log_sample("C-deadbe", 1)

    def test_cluster_id_is_stable_across_queries(self):
        """同一个错误换个窗口再查，ID 必须相同 —— 否则没法跨查询对照。"""
        rec = _Recorder(LOKI_RESPONSE)
        backend = self._backend(rec)
        first = backend.query_logs("orders-api", "15m")[0].cluster_id
        second = backend.query_logs("orders-api", "1h")[0].cluster_id
        assert first == second

    def test_stale_cluster_id_does_not_silently_resolve_to_another_cluster(self):
        """位置序号（C-1/C-2）在这里会犯的错：静默错配。

        ERROR 查询给出 C-1，WARN 查询也给出 C-1，而缓存只留最近一次 ——
        于是 agent 用 ERROR 的 ID 拿到了 WARN 的原始行，且毫无察觉。
        签名派生的 ID 让这种情况变成"查不到"，而不是"拿错"。
        """
        error_only = {
            "data": {"result": [{"stream": {}, "values": [["1", "ERROR aaa failed"]]}]}
        }
        warn_only = {
            "data": {"result": [{"stream": {}, "values": [["2", "WARN bbb slow"]]}]}
        }
        rec = _Recorder(error_only)
        client = make_client(rec)
        backend = LokiLogBackend("http://ops.internal", client=client)
        error_id = backend.query_logs("svc", "15m", level="ERROR")[0].cluster_id

        rec.payload = warn_only
        warn_id = backend.query_logs("svc", "15m", level="WARN")[0].cluster_id
        assert error_id != warn_id

        # 过期的 ERROR ID 必须报错，绝不能返回 WARN 的行
        with pytest.raises(OpsError, match="不在最近一次"):
            backend.get_log_sample(error_id, 1)

    def test_cluster_id_derives_from_signature(self):
        """ID 必须是签名的纯函数 —— 这样才有跨查询的稳定性。

        独立算一遍 sha1，而不是调被测代码自己的 helper（那样等于没测）。
        """
        import hashlib

        rec = _Recorder(LOKI_RESPONSE)
        clusters = self._backend(rec).query_logs("orders-api", "15m")
        for c in clusters:
            expected = "C-" + hashlib.sha1(c.signature.encode("utf-8")).hexdigest()[:6]
            assert c.cluster_id == expected

    def test_cluster_ids_are_order_independent(self):
        """同一批日志换个顺序，ID 跟着签名走而不是跟着位置走。"""
        reversed_payload = {
            "data": {"result": list(reversed(LOKI_RESPONSE["data"]["result"]))}
        }
        a = self._backend(_Recorder(LOKI_RESPONSE)).query_logs("orders-api", "15m")
        b = self._backend(_Recorder(reversed_payload)).query_logs("orders-api", "15m")
        assert {c.cluster_id for c in a} == {c.cluster_id for c in b}

    def test_malformed_entries_are_skipped(self):
        payload = {
            "data": {
                "result": [
                    {"stream": {}, "values": [["1"], ["2", "ERROR ok"], "junk", ["3", ""]]}
                ]
            }
        }
        rec = _Recorder(payload)
        clusters = self._backend(rec).query_logs("svc", "5m")
        assert sum(c.count for c in clusters) == 1

    def test_empty_result_is_empty_list(self):
        rec = _Recorder({"data": {"result": []}})
        assert self._backend(rec).query_logs("svc", "5m") == []

    def test_http_error_becomes_ops_error(self):
        rec = _Recorder({"error": "boom"}, status=500)
        with pytest.raises(OpsError, match="Loki 返回 500"):
            self._backend(rec).query_logs("svc", "5m")

    def test_non_json_response_is_readable(self):
        rec = _Recorder(None, raw="<html>gateway timeout</html>")
        with pytest.raises(OpsError, match="不是 JSON"):
            self._backend(rec).query_logs("svc", "5m")

    def test_alerts_capability_is_reported_missing(self):
        """Loki 不该假装能查告警。"""
        rec = _Recorder(LOKI_RESPONSE)
        with pytest.raises(OpsCapabilityMissing, match="AlertmanagerBackend"):
            self._backend(rec).list_alerts()

    def test_requires_base_url(self):
        with pytest.raises(OpsError, match="需要 base_url"):
            LokiLogBackend("")


# ---------------------------------------------------------------------------
# 四、Prometheus
# ---------------------------------------------------------------------------

PROM_RESPONSE = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"__name__": "http_5xx_rate", "pod": "pod-3"},
                "values": [[1759131600, "0.1"], [1759131900, "8.4"], [1759132200, "12.3"]],
            },
            {
                "metric": {"__name__": "http_5xx_rate", "pod": "pod-1"},
                "values": [[1759131600, "0.1"], [1759131900, "0.1"], [1759132200, "0.2"]],
            },
        ]
    },
}


class TestPrometheus:
    def _backend(self, recorder):
        return PrometheusMetricBackend("http://ops.internal", client=make_client(recorder))

    def test_builds_query_range_request(self):
        rec = _Recorder(PROM_RESPONSE)
        self._backend(rec).query_metrics("http_5xx_rate", "15m", "pod")
        params = rec.params()
        assert rec.last.url.path == "/api/v1/query_range"
        assert params["query"] == "http_5xx_rate"
        assert int(params["end"]) > int(params["start"])
        assert int(params["step"]) >= 15

    def test_promql_expression_passes_through(self):
        rec = _Recorder(PROM_RESPONSE)
        expr = 'sum(rate(http_requests_total{code=~"5.."}[5m])) by (pod)'
        self._backend(rec).query_metrics(expr, "15m", "pod")
        assert rec.params()["query"] == expr

    def test_groups_by_label(self):
        rec = _Recorder(PROM_RESPONSE)
        series = self._backend(rec).query_metrics("http_5xx_rate", "15m", "pod")
        assert {s.group for s in series} == {"pod-1", "pod-3"}

    def test_values_and_baseline_are_usable_by_domain_logic(self):
        """解析出来的时序要能直接喂给 MetricSeries.is_anomalous —— 端到端串一下。"""
        rec = _Recorder(PROM_RESPONSE)
        series = self._backend(rec).query_metrics("http_5xx_rate", "15m", "pod")
        by_group = {s.group: s for s in series}
        assert by_group["pod-3"].baseline == 0.1
        assert by_group["pod-3"].latest == 12.3
        assert by_group["pod-3"].is_anomalous() is True
        assert by_group["pod-1"].is_anomalous() is False

    def test_timestamps_are_hhmm(self):
        rec = _Recorder(PROM_RESPONSE)
        series = self._backend(rec).query_metrics("m", "15m", "pod")
        ts, _ = series[0].values[0]
        assert len(ts) == 5 and ts[2] == ":"

    def test_nan_points_are_dropped(self):
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"pod": "p"}, "values": [[1, "NaN"], [2, "1.5"]]},
                    {"metric": {"pod": "q"}, "values": [[1, "NaN"]]},
                ]
            },
        }
        rec = _Recorder(payload)
        series = self._backend(rec).query_metrics("m", "5m", "pod")
        assert [s.group for s in series] == ["p"]  # q 全是 NaN → 整条丢掉
        assert len(series[0].values) == 1

    def test_non_numeric_values_skipped(self):
        payload = {
            "status": "success",
            "data": {"result": [{"metric": {"pod": "p"}, "values": [[1, "abc"], [2, "3"]]}]},
        }
        rec = _Recorder(payload)
        series = self._backend(rec).query_metrics("m", "5m", "pod")
        assert len(series) == 1 and len(series[0].values) == 1
        assert series[0].latest == 3.0

    def test_promql_error_status_raises(self):
        rec = _Recorder({"status": "error", "error": "parse error at char 3"})
        with pytest.raises(OpsError, match="PromQL 执行失败"):
            self._backend(rec).query_metrics("bad(", "5m", "pod")

    def test_group_falls_back_to_all_labels(self):
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"job": "api", "instance": "1.2.3.4:9090"}, "values": [[1, "1"]]}
                ]
            },
        }
        rec = _Recorder(payload)
        series = self._backend(rec).query_metrics("m", "5m", "pod")
        assert "job=api" in series[0].group and "instance=" in series[0].group

    def test_alerts_capability_is_reported_missing(self):
        rec = _Recorder(PROM_RESPONSE)
        with pytest.raises(OpsCapabilityMissing, match="AlertmanagerBackend"):
            self._backend(rec).list_alerts()


# ---------------------------------------------------------------------------
# 五、Alertmanager
# ---------------------------------------------------------------------------

AM_RESPONSE = [
    {
        "labels": {"alertname": "HTTP5xxHigh", "service": "orders-api",
                   "severity": "critical", "region": "cn-east-1"},
        "annotations": {"summary": "5xx 从 0.1% 升到 12.3%"},
        "startsAt": "2026-09-29T10:38:00Z",
    },
    {
        "labels": {"alertname": "DiskFill", "service": "orders-api", "severity": "warning"},
        "annotations": {"summary": "磁盘 91%"},
        "startsAt": "2026-09-29T09:00:00Z",
    },
    {
        "labels": {"alertname": "Other", "service": "payments", "severity": "weird"},
        "annotations": {},
    },
]


class TestAlertmanager:
    def _backend(self, recorder):
        return AlertmanagerBackend("http://ops.internal", client=make_client(recorder))

    def test_parses_alerts(self):
        rec = _Recorder(AM_RESPONSE)
        alerts = self._backend(rec).list_alerts()
        assert rec.last.url.path == "/api/v2/alerts"
        assert [a.alert_id for a in alerts] == ["HTTP5xxHigh", "DiskFill", "Other"]

    def test_sorted_critical_first(self):
        rec = _Recorder(AM_RESPONSE)
        alerts = self._backend(rec).list_alerts()
        assert alerts[0].severity == "critical"
        assert alerts[0].at == "10:38"

    def test_filters(self):
        rec = _Recorder(AM_RESPONSE)
        backend = self._backend(rec)
        assert len(backend.list_alerts(service="orders-api")) == 2
        assert len(backend.list_alerts(severity="warning")) == 1
        assert backend.list_alerts(service="nope") == []

    def test_unknown_severity_downgraded_to_info(self):
        """告警里出现没见过的 severity 时，不能让它漏出三档之外。"""
        rec = _Recorder(AM_RESPONSE)
        other = [a for a in self._backend(rec).list_alerts() if a.alert_id == "Other"][0]
        assert other.severity == "info"

    def test_get_alert(self):
        rec = _Recorder(AM_RESPONSE)
        backend = self._backend(rec)
        assert backend.get_alert("DiskFill").service == "orders-api"
        assert backend.get_alert("missing") is None

    def test_labels_exclude_alertname(self):
        rec = _Recorder(AM_RESPONSE)
        top = self._backend(rec).list_alerts()[0]
        assert top.labels == {"service": "orders-api", "severity": "critical",
                              "region": "cn-east-1"}
        assert top.detail.startswith("5xx")

    def test_non_list_response_raises(self):
        rec = _Recorder({"data": "unexpected"})
        with pytest.raises(OpsError, match="不是告警列表"):
            self._backend(rec).list_alerts()

    def test_logs_capability_is_reported_missing(self):
        rec = _Recorder(AM_RESPONSE)
        with pytest.raises(OpsCapabilityMissing, match="LokiLogBackend"):
            self._backend(rec).query_logs("orders-api", "15m")


# ---------------------------------------------------------------------------
# 六、Composite：路由 + 「未接入」必须报错
# ---------------------------------------------------------------------------


class _FakeLogs:
    def __init__(self):
        self.calls = []

    def query_logs(self, service, window, level="ERROR", pattern=""):
        self.calls.append(("query_logs", service, window))
        return []

    def get_log_sample(self, cluster_id, count):
        self.calls.append(("get_log_sample", cluster_id, count))
        return ["line"]


class _FakeMetrics:
    def query_metrics(self, metric, window, group_by):
        return []


class TestComposite:
    def test_routes_to_the_right_provider(self):
        logs = _FakeLogs()
        backend = CompositeOpsBackend(logs=logs, metrics=_FakeMetrics())
        assert backend.query_logs("orders-api", "15m") == []
        assert logs.calls == [("query_logs", "orders-api", "15m")]

    def test_missing_capability_raises_not_returns_empty(self):
        """核心不变量：没接入 ≠ 没有异常。

        如果这里返回 []，agent 会读成「这个时间点没有部署」并据此错误定因。
        """
        backend = CompositeOpsBackend(logs=_FakeLogs())
        with pytest.raises(OpsCapabilityMissing) as exc:
            backend.list_deploys("orders-api")
        msg = str(exc.value)
        assert "部署" in msg
        assert "不代表" in msg  # 明确告诉模型这不是"没有部署"
        assert "ArgoCD" in msg  # 并告诉使用者该接什么

    def test_every_unwired_method_raises(self):
        backend = CompositeOpsBackend()
        for call in (
            lambda: backend.list_alerts(),
            lambda: backend.get_alert("AL-1"),
            lambda: backend.query_logs("s", "15m"),
            lambda: backend.get_log_sample("C-1", 1),
            lambda: backend.query_metrics("m", "15m", "pod"),
            lambda: backend.list_deploys(),
            lambda: backend.get_service_health("s"),
            lambda: backend.create_incident("s", "t", "critical", "r", "e"),
        ):
            with pytest.raises(OpsCapabilityMissing):
                call()

    def test_miswired_provider_fails_at_construction(self):
        """把 Loki 当指标后端传进来，应该在启动时炸，而不是半夜排查时才发现。"""
        with pytest.raises(OpsError, match="query_metrics"):
            CompositeOpsBackend(metrics=LokiLogBackend("http://x"))

    def test_wired_and_describe(self):
        rec = _Recorder(AM_RESPONSE)
        real = CompositeOpsBackend(
            alerts=AlertmanagerBackend("http://ops.internal", client=make_client(rec)),
            logs=_FakeLogs(),
        )
        assert set(real.wired()) == {"alerts", "logs"}
        text = real.describe()
        assert "告警" in text and "日志" in text
        assert "✓" in text and "✗" in text
        assert "未接入" in text
        assert "部署" in text  # 未接入的也要列出来

    def test_close_propagates_to_providers(self):
        """close() 要能一路传到真正的连接持有者，否则会漏 socket。"""
        closed = []

        class _Closable:
            def list_alerts(self, service="", severity=""):
                return []

            def get_alert(self, alert_id):
                return None

            def close(self):
                closed.append(True)

        backend = CompositeOpsBackend(alerts=_Closable())
        backend.close()
        assert closed == [True]

    def test_close_ignores_providers_without_close(self):
        """没有 close 的后端（比如纯内存假对象）不能让 close() 炸掉。"""
        CompositeOpsBackend(logs=_FakeLogs()).close()  # 不抛异常即通过

    def test_real_backends_compose_end_to_end(self):
        """三个真后端拼起来，走一遍完整排查链路（全走 MockTransport）。"""
        loki = _Recorder(LOKI_RESPONSE)
        prom = _Recorder(PROM_RESPONSE)
        am = _Recorder(AM_RESPONSE)
        backend = CompositeOpsBackend(
            alerts=AlertmanagerBackend("http://ops.internal", client=make_client(am)),
            logs=LokiLogBackend("http://ops.internal", client=make_client(loki)),
            metrics=PrometheusMetricBackend("http://ops.internal", client=make_client(prom)),
        )

        alerts = backend.list_alerts(service="orders-api")
        assert alerts[0].alert_id == "HTTP5xxHigh"

        clusters = backend.query_logs("orders-api", "15m")
        assert clusters[0].count == 5

        series = backend.query_metrics("http_5xx_rate", "15m", "pod")
        anomalous = [s.group for s in series if s.is_anomalous()]
        assert anomalous == ["pod-3"]  # 只有 pod-3 异常 → 指向那次单 pod 发布

        # 部署没接 —— 必须明确报错，而不是安静地返回"没有部署"
        with pytest.raises(OpsCapabilityMissing, match="部署"):
            backend.list_deploys("orders-api")


# ---------------------------------------------------------------------------
# 七、和工具层对接：MockOpsBackend 与真实后端接口一致
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_mock_satisfies_every_protocol_method(self):
        from mewcode.tools.ops.backend import MockOpsBackend

        protocol = [
            "list_alerts", "get_alert", "query_logs", "get_log_sample",
            "query_metrics", "list_deploys", "get_service_health", "create_incident",
        ]
        mock = MockOpsBackend()
        for name in protocol:
            assert callable(getattr(mock, name)), name

    def test_real_backends_match_protocol_signatures(self):
        """真后端的参数名必须和工具层调用一致，否则会 TypeError。"""
        import inspect

        from mewcode.tools.ops.backend import OpsBackend

        for cls in (LokiLogBackend, PrometheusMetricBackend, AlertmanagerBackend):
            for name, method in inspect.getmembers(cls, inspect.isfunction):
                if name.startswith("_"):
                    continue
                proto = getattr(OpsBackend, name, None)
                if proto is None:
                    continue
                want = list(inspect.signature(proto).parameters)
                got = list(inspect.signature(method).parameters)
                assert got[: len(want)] == want, f"{cls.__name__}.{name}: {got} != {want}"

    def test_json_roundtrip_of_loki_payload_is_stable(self):
        """顺手确认测试用的常量确实是合法 JSON（防止手改后悄悄坏掉）。"""
        assert json.loads(json.dumps(LOKI_RESPONSE)) == LOKI_RESPONSE

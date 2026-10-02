"""真实运维后端：Loki（日志）/ Prometheus（指标）/ Alertmanager（告警）。

这三个都是**标准化 HTTP API**，所以可以写通用实现，不用针对每家改。
`MockOpsBackend` 只用于演示和测试；这里是能接真东西的版本。

## 为什么一个后端只实现一部分方法

`OpsBackend` 有 8 个方法，但**没有任何一个系统同时提供它们**：

    Loki         → 日志
    Prometheus   → 指标
    Alertmanager → 告警
    ArgoCD / CI  → 部署
    工单系统      → 建单

所以每个后端只实现自己那部分，未实现的抛 `OpsCapabilityMissing`，
由 `CompositeOpsBackend` 按方法路由。

## ⚠️ 关键设计：未接入的能力要**抛错**，不能返回空

如果"部署信息没接入"返回空列表，agent 会读成「这个时间点没有部署」——
于是把根因判断到完全错误的方向，而且全程没有任何异常提示。

**静默的空结果在故障排查里比报错危险得多。**

## 阻塞说明

`OpsBackend` 是同步接口（`tools/ops/tools.py` 直接调用），所以这里的
`httpx.Client` 也是同步的 —— 一次查询会短暂阻塞事件循环。
超时默认压到 15s 并**必须在构造时显式给 base_url**，避免默认打到公网。
如果以后要放进高并发场景，把这三个类改成 `httpx.AsyncClient` 并让
`OpsBackend` 的方法变 async 即可，工具层只是多几个 `await`。

## ⚠️ trust_env 默认关掉（踩过的坑）

httpx 默认 `trust_env=True`，会去读系统代理。注意它读的不只是环境变量 ——
在 Windows 上 httpx 0.28 会调 `urllib.request.getproxies()`，**它读注册表**
（`HKCU\\...\\Internet Settings`）。

后果：本机装了 Clash / v2ray（监听 7890 那类）时，对 `127.0.0.1` 的请求
也会被发到代理，代理回 **502 空响应**，报错信息里什么线索都没有。
实测踩过：裸 socket 拿 200，httpx 拿 502，两个请求的差别只有 `trust_env`。

对内部运维系统来说走代理本来就是错的，两个理由：

1. **会坏** —— `loki.internal` 这类内网地址代理根本解析不了
2. **会漏** —— 查询语句和 Bearer token 都会经过第三方

所以默认 `trust_env=False`。确实需要走代理访问公网托管的服务
（比如 Grafana Cloud）时，显式传 `trust_env=True`。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from mewcode.tools.ops.backend import Alert, LogCluster, MetricSeries, OpsError


# ---------------------------------------------------------------------------
# 未接入的能力
# ---------------------------------------------------------------------------


class OpsCapabilityMissing(OpsError):
    """这项运维能力没有接入。

    刻意**抛错而不是返回空** —— 返回空会让 agent 把"没接入"读成"没有异常"，
    从而在毫无提示的情况下把根因判断到错误方向。
    """


# ---------------------------------------------------------------------------
# 公共 HTTP 封装
# ---------------------------------------------------------------------------


class _HttpOpsBackend:
    """三个后端共用。

    `client` 可注入 —— 测试用 `httpx.MockTransport` 就能在不起服务的前提下
    验证真实的请求构造和响应解析（这是这套代码能测的关键）。
    """

    capability: str = "ops"

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
        trust_env: bool = False,
    ) -> None:
        if not base_url:
            raise OpsError(f"{type(self).__name__} 需要 base_url")
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # client 由外部注入时，base_url/headers/trust_env 由调用方负责
        # （MockTransport 和测试场景都走这条）
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            # 见模块 docstring：默认绕开系统代理，否则内网地址会被
            # 发到代理并拿到一个没有任何线索的 502
            trust_env=trust_env,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "_HttpOpsBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise OpsError(f"{self.capability} 请求失败：{e}") from e
        if resp.status_code >= 400:
            raise OpsError(f"{self.capability} 返回 {resp.status_code}：{resp.text[:200]}")
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise OpsError(f"{self.capability} 返回的不是 JSON：{resp.text[:200]}") from e


def _missing(backend: str, method: str, hint: str = "") -> OpsCapabilityMissing:
    msg = f"{backend} 后端没有接入 `{method}` 能力。" + (hint or "")
    return OpsCapabilityMissing(msg)


# ---------------------------------------------------------------------------
# 时间窗口
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(window: str) -> timedelta:
    """把 `15m` / `1h` / `2d` 解析成 timedelta。"""
    m = _WINDOW_RE.match(window.strip())
    if m is None:
        raise OpsError(f"无法解析时间窗口 {window!r}，应为 15m / 1h / 2d 这种形式")
    return timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])


def window_bounds(window: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - parse_window(window), now


# ---------------------------------------------------------------------------
# 日志签名归一化（聚类的核心）
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\S*[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\S*\s*")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
#: 版本号要在普通数字之前处理，否则 `v2.14.3` 只会被换掉第一段
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){2,}\b")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.I)
_QUOTED_RE = re.compile(r'"[^"]*"' + r"|'[^']*'")
_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")
#: ⚠️ 不要用 `\b\d+\b`：`3000ms` 里 `0` 和 `m` 之间**没有**词边界
#: （都是 \w），所以 `\b` 会匹配失败，`3000ms` 原样留下。
#: 而真实日志里 `3000ms` / `1.5GB` / `15s` / `1204req` 遍地都是 ——
#: 一旦它们不归一化，同一类错误会裂成几千个聚类，聚类就等于没做。
#: 正确做法是只要求「前后不是数字」。
_NUM_RE = re.compile(r"(?<![0-9.])\d+(?:\.\d+)?(?![0-9])")
_LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", re.I)
_WS_RE = re.compile(r"\s+")


def log_signature(line: str, max_len: int = 140) -> str:
    """从一行日志里提取"错误签名"，用于聚类。

    去掉所有**每次都不同**的东西（时间戳、ID、数字、地址、路径、版本），
    只留结构 —— 这样同一类错误落到同一个签名上，1,204 条日志才能聚成 1 类
    而不是 1,204 行。

    顺序有讲究：先长模式后短模式（IP 和版本号要在普通数字之前，
    UUID 要在十六进制之前），否则 `10.0.0.7` 会先被数字规则打碎成
    `<N>.<N>.<N>.<N>`。
    """
    s = line.strip()
    s = _TS_RE.sub("", s)
    s = _UUID_RE.sub("<UUID>", s)
    s = _IP_RE.sub("<IP>", s)
    s = _VERSION_RE.sub("<VER>", s)
    s = _HEX_RE.sub("<HEX>", s)
    s = _QUOTED_RE.sub('"<S>"', s)
    s = _PATH_RE.sub("<PATH>", s)
    s = _NUM_RE.sub("<N>", s)
    s = _LEVEL_RE.sub("<L>", s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:max_len]


def _ns_to_hms(ns: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# Loki
# ---------------------------------------------------------------------------


class LokiLogBackend(_HttpOpsBackend):
    """日志后端，对应 `GET /loki/api/v1/query_range`。

    认证传 Bearer token。**建议用只读 token** —— 运维动作的裁判延迟很长，
    回滚/重启这类动作一旦由 AI 直接执行，错了就是二次故障。
    """

    capability = "Loki"

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
        trust_env: bool = False,
        limit: int = 5000,
    ) -> None:
        super().__init__(base_url, token, timeout, client, trust_env)
        self._limit = limit
        # cluster_id -> 该聚类的原始行。Loki 的查询是无状态的，
        # 要让 GetLogSample 能按聚类回取，就在本进程内缓存一份（有上限）。
        self._cache: dict[str, LogCluster] = {}

    def query_logs(
        self, service: str, window: str, level: str = "ERROR", pattern: str = ""
    ) -> list[LogCluster]:
        from_ts, to_ts = window_bounds(window)
        selectors = [f'service="{service}"'] if service else []
        if level:
            # 正则匹配而不是等值：真实日志里是 ERROR / error / ERROR_LOG 混着的
            selectors.append(f'level=~"(?i){level}"')
        logql = "{" + ",".join(selectors) + "}"
        if pattern:
            logql += f' |~ "(?i){pattern}"'

        data = self._get(
            "/loki/api/v1/query_range",
            {
                "query": logql,
                "start": int(from_ts.timestamp() * 1e9),
                "end": int(to_ts.timestamp() * 1e9),
                "limit": self._limit,
                "direction": "backward",
            },
        )
        clusters = _cluster_loki(data)
        self._cache = {c.cluster_id: c for c in clusters}
        return clusters

    def get_log_sample(self, cluster_id: str, count: int) -> list[str]:
        c = self._cache.get(cluster_id)
        if c is None:
            raise OpsError(
                f"日志聚类 {cluster_id} 不在最近一次 QueryLogs 的结果里。"
                "聚类 ID 由日志签名决定（同一个错误在不同窗口里 ID 相同），"
                "所以这通常意味着这个错误不在本次查询的时间范围/过滤条件内 —— "
                "请先调用 QueryLogs 取回聚类列表。"
            )
        return c.sample[: max(1, count)]

    # 明确声明没实现的能力，避免"看起来能用"
    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]:
        raise _missing("Loki", "list_alerts", "请接 AlertmanagerBackend。")

    def get_alert(self, alert_id: str) -> Alert | None:
        raise _missing("Loki", "get_alert", "请接 AlertmanagerBackend。")


def _cluster_id(signature: str) -> str:
    """聚类 ID 由**签名**派生，而不是按位置编号。

    用位置编号（C-1 / C-2）有个隐蔽的坑：`GetLogSample` 是按 ID 查缓存的，
    而缓存只保留最近一次 `QueryLogs` 的结果。查完 ERROR 再查 WARN，
    ERROR 的 `C-1` 就会**静默**解析成 WARN 的 `C-1` ——
    agent 拿到的是另一个聚类的原始行，而且完全不会察觉。

    由签名派生之后：

      · 同一个错误在不同窗口 / 不同查询里 ID **相同**（可以跨查询对照）
      · 过期的 ID 会**查不到**（报错），而不是错配到别的聚类

    6 位十六进制在几百个聚类的量级上足够避开碰撞。
    """
    return "C-" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:6]


def _cluster_loki(data: Any) -> list[LogCluster]:
    """把 Loki 响应聚成 LogCluster。

    Loki 的响应形状：
        {"data": {"result": [{"stream": {...}, "values": [["<ns>", "line"], ...]}]}}
    同一个 stream 里的 `values` 按时间**倒序**（因为我们传了 direction=backward）。
    """
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "first": "", "last": "", "sample": [], "services": set()}
    )
    result = (data or {}).get("data") or {}
    for stream in result.get("result") or []:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") or {}
        svc = labels.get("service") or labels.get("app") or ""
        for entry in stream.get("values") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            ns, line = entry[0], entry[1]
            sig = log_signature(str(line))
            if not sig:
                continue
            g = groups[sig]
            g["count"] += 1
            ts = _ns_to_hms(ns)
            if ts:
                if not g["first"] or ts < g["first"]:
                    g["first"] = ts
                if not g["last"] or ts > g["last"]:
                    g["last"] = ts
            if len(g["sample"]) < 3:
                g["sample"].append(str(line)[:400])
            if svc:
                g["services"].add(svc)

    ordered = sorted(groups.items(), key=lambda kv: kv[1]["count"], reverse=True)
    return [
        LogCluster(
            cluster_id=_cluster_id(sig),
            signature=sig,
            count=g["count"],
            first_seen=g["first"],
            last_seen=g["last"],
            sample=g["sample"],
            services=sorted(g["services"]),
        )
        for sig, g in ordered
    ]


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


class PrometheusMetricBackend(_HttpOpsBackend):
    """指标后端，对应 `GET /api/v1/query_range`。

    `metric` 按 **PromQL 表达式**处理 —— 裸指标名（`http_5xx_rate`）或
    聚合表达式（`sum(rate(http_requests_total{code=~"5.."}[5m]))`）都行。
    `group_by` 决定把返回的多条时序按哪个标签分组建 key。
    """

    capability = "Prometheus"

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
        trust_env: bool = False,
        points: int = 12,
    ) -> None:
        super().__init__(base_url, token, timeout, client, trust_env)
        self._points = max(4, points)

    def query_metrics(self, metric: str, window: str, group_by: str) -> list[MetricSeries]:
        from_ts, to_ts = window_bounds(window)
        span = max(int((to_ts - from_ts).total_seconds()), 1)
        # 至少 4 个点 —— `MetricSeries.baseline`（窗口第一个点）和
        # `latest` 要能分开，"基线 vs 现在"才有意义
        step = max(span // self._points, 15)

        data = self._get(
            "/api/v1/query_range",
            {
                "query": metric,
                "start": int(from_ts.timestamp()),
                "end": int(to_ts.timestamp()),
                "step": step,
            },
        )
        if isinstance(data, dict) and data.get("status") == "error":
            raise OpsError(f"PromQL 执行失败：{data.get('error') or '未知错误'}")

        series: list[MetricSeries] = []
        result = (data or {}).get("data") or {}
        for r in result.get("result") or []:
            if not isinstance(r, dict):
                continue
            labels = r.get("metric") or {}
            group = labels.get(group_by) or _labels_to_key(labels) or "total"
            values: list[tuple[str, float]] = []
            for pair in r.get("values") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                try:
                    ts, val = float(pair[0]), float(pair[1])
                except (TypeError, ValueError):
                    continue
                # Prometheus 用 NaN 表示"这个点位没有数据"，直接丢掉
                if val != val:
                    continue
                values.append(
                    (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M"), val)
                )
            if values:
                series.append(MetricSeries(name=metric, group=group, values=values))
        return series

    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]:
        raise _missing(
            "Prometheus", "list_alerts", "指标后端不提供告警，请接 AlertmanagerBackend。"
        )

    def get_alert(self, alert_id: str) -> Alert | None:
        raise _missing("Prometheus", "get_alert", "请接 AlertmanagerBackend。")


def _labels_to_key(labels: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "__name__")


# ---------------------------------------------------------------------------
# Alertmanager
# ---------------------------------------------------------------------------


class AlertmanagerBackend(_HttpOpsBackend):
    """告警后端，对应 `GET /api/v2/alerts`。"""

    capability = "Alertmanager"
    _SEVERITIES = ("critical", "warning", "info")

    def _fetch(self) -> list[Alert]:
        data = self._get("/api/v2/alerts", {"active": "true", "silenced": "false"})
        if data is None:
            return []
        if not isinstance(data, list):
            raise OpsError("Alertmanager 返回的不是告警列表")
        out: list[Alert] = []
        for i, a in enumerate(data):
            if not isinstance(a, dict):
                continue
            labels = a.get("labels") or {}
            name = labels.get("alertname") or f"unnamed-{i + 1}"
            raw_sev = (labels.get("severity") or "info").lower()
            out.append(
                Alert(
                    alert_id=labels.get("alertname") or f"AL-{i + 1}",
                    service=labels.get("service") or labels.get("job") or "",
                    name=name,
                    severity=raw_sev if raw_sev in self._SEVERITIES else "info",
                    at=_iso_to_hm(a.get("startsAt")),
                    detail=(a.get("annotations") or {}).get("summary", ""),
                    labels={k: str(v) for k, v in labels.items() if k != "alertname"},
                )
            )
        return out

    def list_alerts(self, service: str = "", severity: str = "") -> list[Alert]:
        out = self._fetch()
        if service:
            out = [a for a in out if a.service == service]
        if severity:
            out = [a for a in out if a.severity == severity]
        order = {"critical": 0, "warning": 1, "info": 2}
        out.sort(key=lambda a: (order.get(a.severity, 9), a.at))
        return out

    def get_alert(self, alert_id: str) -> Alert | None:
        return next((a for a in self._fetch() if a.alert_id == alert_id), None)

    def query_logs(
        self, service: str, window: str, level: str = "ERROR", pattern: str = ""
    ) -> list[LogCluster]:
        raise _missing("Alertmanager", "query_logs", "请接 LokiLogBackend。")

    def query_metrics(self, metric: str, window: str, group_by: str) -> list[MetricSeries]:
        raise _missing("Alertmanager", "query_metrics", "请接 PrometheusMetricBackend。")


def _iso_to_hm(raw: Any) -> str:
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return ""

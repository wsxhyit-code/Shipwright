"""运维工具集：告警 / 日志 / 指标 / 部署 / 健康 / 故障工单。

## 权限映射（沿用现有三分类，零权限代码改动）

    查询类   → category="read"   → DEFAULT 下 allow    ← 运维的主战场，不该弹窗
    建工单   → category="write"  → DEFAULT 下 ask      ← 唯一的写操作，必须确认

**刻意不提供"回滚部署""重启服务"这类工具** —— 那些属于发布运维，
应该由 CI 的确定性流水线做（有灰度、有审批、有回滚策略），
让 LLM 直接触发是不可接受的。

## 为什么不直接把原始日志返回

`QueryLogs` 返回的是**聚类**（错误签名 + 条数 + 时间范围 + 一行样例）。
如果返回 1,204 条原始日志，就是 240KB 进上下文 —— 正好落进
"结果超限→落盘→只给 2KB 预览"那个坑。

需要细节时用 `GetLogSample` 按聚类取少量原始行，对应
"Grep 定位 → ReadFile 局部读" 的思路。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.ops.backend import OpsBackend, OpsError


class _OpsTool(Tool):
    """统一把业务异常转成可读的 ToolResult，让模型自己调整。"""

    def __init__(self, backend: OpsBackend) -> None:
        self._backend = backend

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------


class ListAlertsParams(BaseModel):
    service: str = Field(default="", description="服务名；留空表示全部")
    severity: str = Field(default="", description="critical / warning / info")


class ListAlertsTool(_OpsTool):
    name = "ListAlerts"
    description = (
        "列出当前告警，按严重程度排序。**排查线上问题的第一步。**\n"
        "拿到 critical 告警后，先用 GetAlert 看详情，再按告警指向的服务去查日志和指标。"
    )
    params_model = ListAlertsParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ListAlertsParams = params  # type: ignore[assignment]
        alerts = self._backend.list_alerts(p.service, p.severity)
        if not alerts:
            return ToolResult(output="当前没有告警")
        lines = [f"当前告警（{len(alerts)} 条，按严重程度）"]
        for a in alerts:
            lines.append(
                f"  [{a.severity}] {a.alert_id}  {a.service}  {a.at}\n"
                f"        {a.name} — {a.detail}"
            )
        return ToolResult(output="\n".join(lines))


class GetAlertParams(BaseModel):
    alert_id: str


class GetAlertTool(_OpsTool):
    name = "GetAlert"
    description = "查单条告警的详情与标签（环境、区域等）。"
    params_model = GetAlertParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: GetAlertParams = params  # type: ignore[assignment]
        a = self._backend.get_alert(p.alert_id)
        if a is None:
            return ToolResult(output=f"告警 {p.alert_id} 不存在", is_error=True)
        labels = ", ".join(f"{k}={v}" for k, v in a.labels.items()) or "-"
        return ToolResult(
            output=(
                f"{a.alert_id}  [{a.severity}]  {a.service}\n"
                f"  名称：{a.name}\n"
                f"  时间：{a.at}\n"
                f"  详情：{a.detail}\n"
                f"  标签：{labels}"
            )
        )


# ---------------------------------------------------------------------------
# 日志（聚类，不是全量）
# ---------------------------------------------------------------------------


class QueryLogsParams(BaseModel):
    service: str
    window: str = Field(default="15m", description="时间窗口，如 15m / 1h")
    level: str = Field(default="ERROR", description="ERROR / WARN / INFO")
    pattern: str = Field(default="", description="按关键词过滤错误签名")


class QueryLogsTool(_OpsTool):
    name = "QueryLogs"
    description = (
        "查询服务日志，**返回按错误签名聚合的聚类**（类型 / 条数 / 时间范围 / 一行样例），"
        "不是原始日志。这样一次调用就能看出「主要是什么错、从什么时候开始」。\n"
        "需要看某个聚类的原始堆栈时，用 GetLogSample(cluster_id)。"
    )
    params_model = QueryLogsParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: QueryLogsParams = params  # type: ignore[assignment]
        clusters = self._backend.query_logs(p.service, p.window, p.level, p.pattern)
        if not clusters:
            return ToolResult(
                output=f"{p.service} 最近 {p.window} 没有 {p.level} 级日志"
            )
        total = sum(c.count for c in clusters)
        lines = [
            f"{p.service} 日志聚类（最近 {p.window}，{p.level}，"
            f"共 {total:,} 条 / {len(clusters)} 类）",
            "",
        ]
        for c in clusters:
            lines.append(
                f"  [{c.cluster_id}] {c.signature}\n"
                f"      {c.count:,} 条（{c.count / total:.0%}）  "
                f"首次 {c.first_seen}  最后 {c.last_seen}"
            )
            if c.sample:
                lines.append(f"      样例：{c.sample[0][:110]}")
        lines.append("")
        lines.append("提示：要定位根因，用 GetLogSample 拉某个聚类的完整堆栈；")
        lines.append("      再用 ListDeploys 看这个时间点前后有没有发布。")
        return ToolResult(output="\n".join(lines))


class GetLogSampleParams(BaseModel):
    cluster_id: str
    count: int = Field(default=5, ge=1, le=20)


class GetLogSampleTool(_OpsTool):
    name = "GetLogSample"
    description = (
        "取某个日志聚类的原始行（含完整堆栈），有数量上限。\n"
        "**只在你已经通过 QueryLogs 定位到具体聚类之后才调用**，不要一上来就拉全量日志。"
    )
    params_model = GetLogSampleParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: GetLogSampleParams = params  # type: ignore[assignment]
        try:
            rows = self._backend.get_log_sample(p.cluster_id, p.count)
        except OpsError as e:
            return ToolResult(output=str(e), is_error=True)
        if not rows:
            return ToolResult(output=f"聚类 {p.cluster_id} 没有可用样例")
        return ToolResult(
            output=f"聚类 {p.cluster_id} 原始日志（{len(rows)} 行）\n" + "\n".join(rows)
        )


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


class QueryMetricsParams(BaseModel):
    metric: str = Field(description="指标名，如 http_5xx_rate / http_p99_ms / cpu_usage")
    window: str = Field(default="30m")
    group_by: str = Field(default="pod", description="分组键")


class QueryMetricsTool(_OpsTool):
    name = "QueryMetrics"
    description = (
        "查指标时序并**对比基线**：给出每个分组的最新值、窗口起始值、变化幅度。\n"
        "关键用法是看**分组之间的差异** —— 如果只有某一个 pod 异常，"
        "那问题几乎肯定在那个 pod 上（例如它刚被发布过）。"
    )
    params_model = QueryMetricsParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: QueryMetricsParams = params  # type: ignore[assignment]
        try:
            series = self._backend.query_metrics(p.metric, p.window, p.group_by)
        except OpsError as e:
            return ToolResult(output=str(e), is_error=True)
        if not series:
            return ToolResult(output=f"指标 {p.metric} 在最近 {p.window} 没有数据")

        lines = [f"{p.metric}（最近 {p.window}，按 {p.group_by} 分组）", ""]
        anomalous: list[str] = []
        for s in series:
            base, now = s.baseline, s.latest
            if s.is_anomalous():
                anomalous.append(s.group)
                change = f"⚠️ ↑ {now / base:.1f}×"
            elif base > 0:
                change = f"→ {(now - base) / base:+.1%}"
            else:
                change = "→ 无基线"
            lines.append(f"  {s.group:<10} {base:g} → {now:g}   {change}")
        if anomalous:
            lines.append("")
            lines.append(f"  ⚠️ 异常分组：{', '.join(anomalous)}（其余分组平稳）")
            lines.append("  → 建议查这个分组上的最近部署：ListDeploys")
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------


class ListDeploysParams(BaseModel):
    service: str = ""
    limit: int = Field(default=10, ge=1, le=50)


class ListDeploysTool(_OpsTool):
    name = "ListDeploys"
    description = (
        "查最近的部署记录（版本 / 时间 / 目标实例 / commit / 作者）。\n"
        "**故障定位里最关键的一步**：如果错误开始时间和某次部署接近，"
        "基本可以锁定是这次发布引入的；拿到 commit 后用 ReadFile / Grep 去看代码改动。"
    )
    params_model = ListDeploysParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ListDeploysParams = params  # type: ignore[assignment]
        deploys = self._backend.list_deploys(p.service, p.limit)
        if not deploys:
            return ToolResult(output="没有部署记录")
        lines = [f"{p.service or '全部服务'} 最近部署"]
        for d in deploys:
            lines.append(
                f"  {d.at}  {d.version:<10} → {', '.join(d.targets) or '-'}"
                f"   commit={d.commit or '-'}  by {d.author or '-'}"
            )
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# 健康 & 时间线
# ---------------------------------------------------------------------------


class GetServiceHealthParams(BaseModel):
    service: str


class GetServiceHealthTool(_OpsTool):
    name = "GetServiceHealth"
    description = "查服务的整体健康状态与各实例状态。"
    params_model = GetServiceHealthParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: GetServiceHealthParams = params  # type: ignore[assignment]
        try:
            h = self._backend.get_service_health(p.service)
        except OpsError as e:
            return ToolResult(output=str(e), is_error=True)
        pods = h.get("pods", {})
        lines = [
            f"{h['service']}  状态={h['status']}",
            f"  实例：{', '.join(f'{k}={v}' for k, v in pods.items())}",
        ]
        if h.get("note"):
            lines.append(f"  备注：{h['note']}")
        return ToolResult(output="\n".join(lines))


class BuildTimelineParams(BaseModel):
    service: str
    window: str = Field(default="30m")


class BuildTimelineTool(_OpsTool):
    name = "BuildTimeline"
    description = (
        "把该服务的**告警时间、部署时间、各日志聚类的首次出现时间、指标拐点**"
        "排到一条时间线上（只做机械排列，不做结论）。\n"
        "用途：一次调用拿到事件的先后顺序，省掉多次查询。\n"
        "**结论要你自己下** —— 这个工具只负责把线索按时间摆好。"
    )
    params_model = BuildTimelineParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: BuildTimelineParams = params  # type: ignore[assignment]
        events: list[tuple[str, str]] = []

        for a in self._backend.list_alerts(p.service):
            events.append((a.at, f"[告警] {a.severity} {a.name}（{a.alert_id}）"))
        for d in self._backend.list_deploys(p.service):
            events.append(
                (d.at, f"[部署] {d.version} → {', '.join(d.targets)} commit={d.commit}")
            )
        try:
            for c in self._backend.query_logs(p.service, p.window):
                events.append(
                    (c.first_seen[:5], f"[日志首现] {c.signature[:70]}（{c.count:,} 条）")
                )
        except OpsError:
            pass
        for metric in ("http_5xx_rate", "http_p99_ms"):
            try:
                series = self._backend.query_metrics(metric, p.window, "pod")
            except OpsError:
                continue
            for s in series:
                base = s.baseline
                if base <= 0:
                    continue
                for ts, val in s.values:
                    if val > base * 2:
                        events.append(
                            (ts, f"[指标拐点] {metric}@{s.group} {base:g} → {val:g}")
                        )
                        break

        if not events:
            return ToolResult(output=f"{p.service} 在最近 {p.window} 没有可对齐的事件")

        events.sort(key=lambda e: e[0])
        lines = [f"{p.service} 事件时间线（最近 {p.window}）", ""]
        for ts, desc in events:
            lines.append(f"  {ts}  {desc}")
        lines += [
            "",
            "注意：以上只是**按时间排列的事实**，因果关系需要你结合代码和上下文判断。",
        ]
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# 建故障工单（唯一的写操作）
# ---------------------------------------------------------------------------


class CreateIncidentParams(BaseModel):
    service: str
    title: str
    severity: str = Field(description="critical / major / minor")
    root_cause: str = Field(description="根因结论")
    evidence: str = Field(
        description="证据链：日志聚类 ID、指标数据、部署版本、代码位置等具体线索"
    )


class CreateIncidentTool(_OpsTool):
    name = "CreateIncident"
    description = (
        "登记故障工单。**必须提供证据链** —— 系统会拒绝没有依据的根因。\n"
        "证据要具体到：哪个日志聚类、哪个指标、哪次部署、哪个文件哪一行。\n"
        "只写「服务异常」这种没有信息量的话会被拒绝。"
    )
    params_model = CreateIncidentParams
    category = "write"      # DEFAULT 模式下落到"人工确认"
    is_concurrency_safe = False

    async def execute(self, params: BaseModel) -> ToolResult:
        p: CreateIncidentParams = params  # type: ignore[assignment]
        try:
            inc = self._backend.create_incident(
                p.service, p.title, p.severity, p.root_cause, p.evidence
            )
        except OpsError as e:
            return ToolResult(output=f"建单被拒：{e}", is_error=True)
        return ToolResult(
            output=(
                f"已登记故障 {inc.incident_id}\n"
                f"  服务：{inc.service}   严重度：{inc.severity}\n"
                f"  标题：{inc.title}\n"
                f"  根因：{inc.root_cause}"
            )
        )


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

OPS_TOOLS: tuple[type[_OpsTool], ...] = (
    ListAlertsTool,
    GetAlertTool,
    QueryLogsTool,
    GetLogSampleTool,
    QueryMetricsTool,
    ListDeploysTool,
    GetServiceHealthTool,
    BuildTimelineTool,
    CreateIncidentTool,
)


def register_ops_tools(registry, backend: OpsBackend) -> list[str]:
    for cls in OPS_TOOLS:
        registry.register(cls(backend))
    return [cls.name for cls in OPS_TOOLS]

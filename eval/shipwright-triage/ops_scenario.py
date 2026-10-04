"""把内置 MockOpsBackend 换成「account-api 真实故障」场景。

## 为什么需要这个脚本

`tools/ops/backend.py` 里的 `MockOpsBackend` 自带一个演示场景
（orders-api / pod-3 / NPE），那是写死在代码里的**假代码**：demo 脚本会临时
造一个 `OrderService.java`，跑完就删。所以"agent 读代码定位到那一行"这一步
其实是对着一个刚生成的玩具文件做的。

这个脚本换成真场景：

  · 告警/日志/指标/部署数据里的**每一处代码引用**（文件名、行号、报错文本）
    都是从**真实运行的** account-api 上用 `traceback.format_exc()` 抓出来的，
    不是手写的 —— 所以数据和代码不可能对不上
  · 被引用的代码是仓库里真实存在的、有测试的、可复现的服务

用法（先设好 PYTHONPATH，见 eval/shipwright-triage/run_triage.ps1）：

    python eval/shipwright-triage/ops_scenario.py            # 打印场景，零 AI

之后用 `--scenario` 参数挂到 agent 上，它会通过 QueryLogs / ListDeploys /
ReadFile 自己去发现这条链路。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 让这个脚本能独立运行（也能被 run_triage.ps1 以 --scenario 方式加载）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mewcode.tools.ops.backend import (  # noqa: E402
    Alert,
    Deploy,
    LogCluster,
    MetricSeries,
    MockOpsBackend,
)

APP_ROOT = Path(__file__).resolve().parent / "account-api"
APP_SRC = APP_ROOT / "src"
APP_FILE = APP_SRC / "account_api" / "policy.py"

#: 触发故障的那个用户（420 分：基础口径 GOLD，VIP 口径 STANDARD）
TRIGGER_USER = "u-1002"

#: 故障开始时间（部署后 12 分钟）
INCIDENT_AT = "10:23"


def _trigger_traceback() -> str:
    """真的把故障跑一遍，抓真实堆栈。

    这是整个场景里最关键的一步：日志里的文件名、行号、报错信息全部来自
    这一次真实的异常，而不是我手写的字符串。所以「日志说的那行」和
    「代码里那行」永远对得上。
    """
    if str(APP_SRC) not in sys.path:
        sys.path.insert(0, str(APP_SRC))

    from account_api.policy import clear_cache  # noqa: PLC0415
    from account_api.service import account_summary  # noqa: PLC0415

    clear_cache()
    try:
        account_summary(TRIGGER_USER)
    except Exception:  # noqa: BLE001 —— 要的就是这个异常
        return traceback.format_exc()
    raise RuntimeError(
        f"场景失效：{TRIGGER_USER} 这次没有触发异常。"
        "要么 bug 已被修好（那就不需要这个场景了），要么测试数据被改过。"
    )


def _deepest_app_frame(tb_text: str) -> str:
    """从堆栈里挑出**最内层**属于业务代码的那一帧 —— 也就是断言失败的位置。

    挑最内层而不是最外层：外层帧只是调用者，真正出错的是最里面那一行。
    """
    frames = [
        line.strip()
        for line in tb_text.splitlines()
        if line.strip().startswith('File "') and "account_api" in line
    ]
    return frames[-1] if frames else ""


class AccountApiIncidentBackend(MockOpsBackend):
    """account-api 真实故障场景。

    与内置 mock 的区别：这份数据是**从真实代码里生成的**。
    """

    capability = "mock"

    def __init__(self) -> None:
        super().__init__()
        tb = _trigger_traceback()
        frame = _deepest_app_frame(tb)
        error_line = next(
            (ln.strip() for ln in tb.splitlines() if ln.strip().startswith("AssertionError")),
            "AssertionError",
        )
        rel_file = "eval/shipwright-triage/account-api/src/account_api/policy.py"

        self._alerts = [
            Alert(
                alert_id="AL-9014",
                service="account-api",
                name="HTTP 5xx rate high",
                severity="critical",
                at="10:38",
                detail="5xx 占比 5 分钟内从 0.2% 升到 12.4%",
                labels={"region": "cn-east-1", "env": "prod", "team": "growth"},
            ),
            Alert(
                alert_id="AL-9012",
                service="account-api",
                name="P99 latency high",
                severity="warning",
                at="10:29",
                detail="P99 从 120ms 升到 1.9s（失败请求在超时前就返回了）",
                labels={"region": "cn-east-1", "env": "prod", "team": "growth"},
            ),
        ]

        self._logs = {
            "C-1": LogCluster(
                cluster_id="C-1",
                signature=error_line,
                count=1847,
                first_seen="10:23:11",
                last_seen="10:38:04",
                services=["account-api"],
                sample=[
                    f"2026-10-04 10:23:11.402 ERROR [account-api,pod-3] c.a.a.api - "
                    f"unhandled exception",
                    *tb.rstrip().splitlines()[-6:],
                    f"    触发请求：GET /v1/accounts/{TRIGGER_USER}",
                ],
            ),
            "C-2": LogCluster(
                cluster_id="C-2",
                signature="profile-rpc 调用量同比上升 340%（缓存没有起到应有的作用）",
                count=612,
                first_seen="10:24:02",
                last_seen="10:37:50",
                services=["account-api"],
                sample=[
                    "2026-10-04 10:24:02.881 WARN [account-api,pod-3] c.a.a.policy - "
                    "profile RPC qps 380（基线 42）",
                    "2026-10-04 10:24:03.104 WARN [account-api,pod-3] c.a.a.policy - "
                    "tier 判定缓存命中率 0.0%（每个请求都回源读用户资料）",
                ],
            ),
        }

        self._metrics = {
            "http_5xx_rate": {
                "pod-1": MetricSeries("http_5xx_rate", "pod-1", [
                    ("10:20", 0.2), ("10:25", 0.2), ("10:30", 0.2), ("10:38", 0.2)]),
                "pod-2": MetricSeries("http_5xx_rate", "pod-2", [
                    ("10:20", 0.2), ("10:25", 0.2), ("10:30", 0.2), ("10:38", 0.2)]),
                "pod-3": MetricSeries("http_5xx_rate", "pod-3", [
                    ("10:20", 0.2), ("10:25", 0.2), ("10:30", 12.1), ("10:38", 12.4)]),
            },
            "http_p99_ms": {
                "pod-1": MetricSeries("http_p99_ms", "pod-1", [
                    ("10:20", 118), ("10:25", 120), ("10:30", 121), ("10:38", 119)]),
                "pod-3": MetricSeries("http_p99_ms", "pod-3", [
                    ("10:20", 120), ("10:25", 121), ("10:30", 1880), ("10:38", 1902)]),
            },
            "profile_rpc_qps": {
                "pod-3": MetricSeries("profile_rpc_qps", "pod-3", [
                    ("10:20", 42), ("10:25", 43), ("10:30", 380), ("10:38", 395)]),
            },
            "cpu_usage": {
                "pod-3": MetricSeries("cpu_usage", "pod-3", [
                    ("10:20", 0.28), ("10:25", 0.29), ("10:30", 0.30), ("10:38", 0.29)]),
            },
        }

        self._deploys = [
            Deploy("D-7712", "account-api", "v2.14.3", "10:11", ["pod-3"],
                   commit="e3f9a12", author="chenlei"),
            Deploy("D-7709", "account-api", "v2.14.2", "09:02",
                   ["pod-1", "pod-2", "pod-3"], commit="7b41c08", author="liuyang"),
        ]

        self._health = {
            "account-api": {
                "service": "account-api",
                "status": "degraded",
                "pods": {"pod-1": "healthy", "pod-2": "healthy", "pod-3": "unhealthy"},
                "restarts_5m": {"pod-1": 0, "pod-2": 0, "pod-3": 0},
                "note": "pod-3 自 10:23 起在部分用户上持续 500",
            }
        }

        #: 给报告用的原始堆栈（完整，不截断）
        self.raw_traceback = tb
        self.cited_file = rel_file
        self.cited_frame = frame

    def describe(self) -> str:
        return (
            "运维后端能力清单（account-api 故障场景）：\n"
            "  ✓ 告警：AL-9014（critical 5xx）/ AL-9012（warning 延迟）\n"
            "  ✓ 日志：聚类 C-1 / C-2\n"
            "  ✓ 指标：http_5xx_rate / http_p99_ms / profile_rpc_qps / cpu_usage\n"
            "  ✓ 部署：D-7712（v2.14.3 → pod-3）/ D-7709\n"
            "  ✓ 服务健康、工单\n"
            "注意：日志里的代码引用（文件 / 行号 / 报错文本）是从真实运行的"
            "服务上抓的，不是编的。"
        )


def main() -> None:
    backend = AccountApiIncidentBackend()
    os.environ.setdefault("POD_NAME", "pod-3")

    print("=" * 78)
    print("场景自检：日志里的代码引用是不是真的")
    print("=" * 78)
    print(f"被引用的文件（相对仓库根）：{backend.cited_file}")
    print(f"堆栈里属于业务代码的帧：{backend.cited_frame}")
    print()
    print("完整堆栈（就是 QueryLogs / GetLogSample 会给模型看的东西）：")
    print(backend.raw_traceback)

    print("=" * 78)
    print("被引用文件的实际内容（行号对齐检查）")
    print("=" * 78)
    lines = APP_FILE.read_text(encoding="utf-8").splitlines()
    for n, text in enumerate(lines, start=1):
        if "assert tier == verified" in text or "_tier_cache" in text:
            print(f"  {n:4} | {text}")

    print()
    print("=" * 78)
    print("告警")
    print("=" * 78)
    for a in backend.list_alerts():
        print(f"  [{a.severity}] {a.alert_id} {a.service} {a.at} — {a.name}: {a.detail}")
    for c in backend.query_logs("account-api", "15m"):
        print(f"  日志 {c.cluster_id}: {c.signature}（{c.count} 条，首次 {c.first_seen}）")
    for d in backend.list_deploys("account-api"):
        print(f"  部署 {d.at} {d.version} → {', '.join(d.targets)} commit={d.commit}")


if __name__ == "__main__":
    main()

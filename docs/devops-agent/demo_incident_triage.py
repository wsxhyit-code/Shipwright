"""故障定位端到端 demo：按 SOP 走一遍，含**真实的读代码**那一步。

    python docs/devops-agent/demo_incident_triage.py

零 API 成本：运维工具用内存 backend，读代码用真实的 ReadFile / Grep。

它要证明的核心是最后一步 —— **普通运维 AI 只能说到"pod-3 有问题"，
带 coding 能力的 agent 能说到"142 行少了判空"**。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mewcode.tools import create_default_registry  # noqa: E402
from mewcode.tools.ops import MockOpsBackend, register_ops_tools  # noqa: E402

DEMO = Path(__file__).resolve().parent / ".demo-orders-api"

# 有 bug 的那一行，必须落在第 142 行上（和日志里的堆栈对上）
BUG_LINE = 142
BUGGY = "        return user.getProfile().getTier();"


def build_repo() -> None:
    """造一个"出问题的仓库"：OrderService.java 的第 142 行少了判空。"""
    shutil.rmtree(DEMO, ignore_errors=True)
    src = DEMO / "src" / "main" / "java" / "com" / "acme" / "order"
    src.mkdir(parents=True)

    lines = [
        "package com.acme.order;",
        "",
        "import com.acme.user.Profile;",
        "import com.acme.user.User;",
        "",
        "/** 订单服务。getTier 在 v2.14.3 里被改动过。 */",
        "public class OrderService {",
        "    private final UserRepository users;",
        "",
        "    public OrderService(UserRepository users) {",
        "        this.users = users;",
        "    }",
        "",
    ]
    # 用注释把方法体顶到第 142 行
    while len(lines) < BUG_LINE - 4:
        lines.append("    // step " + str(len(lines)) + ": 组装订单行的各个字段")
    lines += [
        "    public String getTier(long orderId) {",
        "        User user = this.users.findById(orderId);",
        "        // v2.14.3 移除了这里的判空（原来是 if (user == null) return \"GUEST\";）",
        BUGGY,
        "    }",
        "}",
    ]
    (src / "OrderService.java").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 另一个文件里还有同类调用点 —— 用来演示"改了一半"的检查
    (src / "OrderController.java").write_text(
        textwrap.dedent(
            """\
            package com.acme.order;

            public class OrderController {
                private final OrderService orders;

                public OrderController(OrderService orders) {
                    this.orders = orders;
                }

                public String list(long orderId) {
                    return this.orders.getTier(orderId);
                }
            }
            """
        ),
        encoding="utf-8",
    )


def banner(title: str, note: str = "") -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    if note:
        print(note + "\n")


async def main() -> None:
    build_repo()
    reg = create_default_registry()
    register_ops_tools(reg, MockOpsBackend())

    async def call(name, **args):
        tool = reg.get(name)
        return await tool.execute(tool.params_model.model_validate(args))

    banner("① ListAlerts —— 先看有哪些告警", "（SOP 第一步：确定影响面）")
    print((await call("ListAlerts")).output)

    banner("② GetAlert —— 看最严重那条的详情", "（拿到服务名 / 环境 / 区域）")
    print((await call("GetAlert", alert_id="AL-7781")).output)

    banner(
        "③ QueryLogs —— 查日志**聚类**（不是原始日志）",
        "（关键设计：返回聚类摘要，一次调用看出「主要是什么错、从何时开始」）",
    )
    print((await call("QueryLogs", service="orders-api", window="15m")).output)

    banner(
        "④ QueryMetrics —— 指标**重点看分组差异** ⭐",
        "（只看总量平均值会错过线索：平均下来可能正常，但一个实例已经全挂）",
    )
    print((await call("QueryMetrics", metric="http_5xx_rate", window="30m")).output)

    banner("⑤ ListDeploys —— 和错误起始时间对比 ⭐", "（拿到 commit，这是进入代码层面的钥匙）")
    print((await call("ListDeploys", service="orders-api", limit=3)).output)

    banner("⑥ BuildTimeline —— 把事件按时间摆好", "（工具只摆事实，因果判断交给模型）")
    print((await call("BuildTimeline", service="orders-api", window="30m")).output)

    # ── ⑦ 读代码：这一步只有带 coding 能力的 agent 能做 ──────────────
    banner(
        "⑦ 读代码 —— ★ 这一步是分水岭 ★",
        "普通运维 AI 到这里只能说「建议重启 pod-3」；\n"
        "带 coding 能力的 agent 会从日志堆栈里的 OrderService.java:142 直接去看代码。",
    )
    grep_r = await call("Grep", pattern="getTier", path=str(DEMO))
    print("→ Grep(pattern='getTier')")
    for line in grep_r.output.splitlines():
        print(f"    {line}")

    target = "src/main/java/com/acme/order/OrderService.java"
    read_r = await call("ReadFile", file_path=str(DEMO / target),
                        offset=BUG_LINE - 4, limit=7)
    print(f"\n→ ReadFile({target}, offset={BUG_LINE - 4}, limit=7)")
    print(textwrap.indent(read_r.output, "    "))

    same_class = await call("Grep", pattern=r"getProfile\(\)\.", path=str(DEMO))
    print(f"\n→ Grep(pattern='getProfile\\\\(\\\\)\\\\.') —— 查有没有同类问题只修了一处")
    print(textwrap.indent(same_class.output or "(仅此一处)", "    "))

    # ── ⑧ 建单（真实流程里这一步需要人工确认）─────────────────────
    banner("⑧ CreateIncident —— 建故障工单", "（写操作，DEFAULT 模式下会落到「人工确认」）")
    inc = await call(
        "CreateIncident",
        service="orders-api",
        title="orders-api 5xx 突增（仅 pod-3）",
        severity="critical",
        root_cause="v2.14.3 移除了 OrderService.getTier 里对 getProfile() 的判空",
        evidence=(
            "日志：聚类 C-1，NullPointerException at OrderService.java:142，"
            "1204 条，首次 10:23:41；"
            "指标：http_5xx_rate 仅 pod-3 从 0.1% 升至 12.3%，其余实例平稳；"
            f"部署：D-5521 v2.14.3 于 10:11 发布到 pod-3，commit 9b2c1f4；"
            f"代码：{target}:{BUG_LINE} 缺判空，且该行在本次发布中被改动"
        ),
    )
    print(inc.output)

    banner("对照：无证据的工单会被拒")
    bad = await call(
        "CreateIncident", service="orders-api", title="服务异常",
        severity="critical", root_cause="代码有问题", evidence="   ",
    )
    print(f"is_error = {bad.is_error}\n{bad.output}")

    banner("清理 demo 目录")
    shutil.rmtree(DEMO, ignore_errors=True)
    print("已清理")


if __name__ == "__main__":
    asyncio.run(main())

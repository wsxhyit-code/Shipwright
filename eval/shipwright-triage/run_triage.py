"""把一个告警丢给 agent，看它能不能自己定位到代码里的那一行。

## 为什么用包装脚本而不是把提示词写在命令行里

  · 提示词里有大量中文和换行 —— 走 PowerShell 命令行会踩编码/引号两个坑
    （`.\run.ps1 -p "..."` 那次实测就踩过 PowerShell 吞参数的问题）
  · 提示词本身是这次实验的**自变量**，值得单独存一个文件、能 diff

用法：
    python eval/shipwright-triage/run_triage.py            # 打印最终结果
    python eval/shipwright-triage/run_triage.py --dry-run  # 只打印提示词
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

APP_DIR = _HERE / "account-api"

TASK = """\
你是 account-api 的 on-call 工程师。现在收到了生产告警：

    服务 account-api 在 10:38 触发 critical 告警：HTTP 5xx 占比在 5 分钟内
    从 0.2% 升到 12.4%。同一时间还有一条 P99 延迟告警。

当前工作目录就是 account-api 的代码仓库。请完成三件事：

1. **定位根因**：用运维工具查清是哪次变更、哪个文件、哪一行导致的。
   根因必须给到具体文件和行号，并说明"为什么只有部分用户出错"。
2. **改代码**：按你的根因判断修改实现，让这份代码不再出错。
3. **提 PR**：改完用 CreatePR 提交。它会先做验证门禁，验证不通过会被打回。

## 规则（重要）

- 交付**只能**走 CreatePR。不要用 Bash 执行 `git push`，也不要自己开 PR。
- 验收口径就是仓库 README 里那条命令：`python -m pytest -q`，退出码必须为 0。
  提交前先自己跑一遍。
- 不要为了让测试变绿而删测试或放宽断言 —— 验证门禁之外还有一层独立验证者
  在审你的改动，它看不到这段对话。
- 结束前，在最终回复里给出：根因（文件:行号）、证据链（哪条告警 / 哪个日志聚类 /
  哪个指标 / 哪次部署）、改了什么、验证结果、以及 CreatePR 的返回内容。
"""


def build_prompt() -> str:
    return TASK


async def run() -> int:
    from mewcode.agent import Agent
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.client import create_client, resolve_context_window
    from mewcode.config import ConfigError, load_config
    from mewcode.conversation import ConversationManager
    from mewcode.hooks import HookConfigError, HookEngine, load_hooks
    from mewcode.memory.instructions import load_instructions
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )
    from mewcode.teams.manager import TeamManager
    from mewcode.toolset import assemble_toolset
    from mewcode.tools import create_default_registry
    from mewcode.tools.agent_tool import AgentTool
    from mewcode.tools.impl.tool_search import ToolSearchTool
    from mewcode.tools.team_create import TeamCreateTool
    from mewcode.tools.team_delete import TeamDeleteTool
    from mewcode.worktree import WorktreeManager

    try:
        config = load_config(_HERE / "config.yaml")
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as exc:
        print(f"hook 配置错误：{exc}", file=sys.stderr)
        return 2
    hook_engine = HookEngine(hooks) if hooks else None

    provider = config.providers[0]
    client = create_client(provider)
    await resolve_context_window(provider)

    # 换掉运维数据源：内置 MockOpsBackend 讲的是 orders-api 的假故事，
    # 这里要换成 account-api 的真实故障场景。装配层是按配置里的 kind 造后端的,
    # 所以在这一层替换掉工厂函数 —— 不动 toolset.py（那是产品代码）。
    if config.toolset is not None and config.toolset.ops:
        # ⚠️ 打补丁的位置很讲究：toolset.py 里写的是
        #     `from ...backends.factory import build_ops_backend`
        #   —— 那是**模块级绑定**，函数对象在 toolset 模块导入时就固定下来了。
        #   改 factory 模块自己的属性没用（实测：装配出来的还是 MockOpsBackend）。
        #   必须打在 toolset 模块的属性上。
        import mewcode.toolset as toolset_module

        from ops_scenario import AccountApiIncidentBackend

        scenario = AccountApiIncidentBackend()

        def _scenario_backend(_specs):
            """无论配置里声明了几个后端，都返回这一个真实故障场景。"""
            return scenario

        toolset_module.build_ops_backend = _scenario_backend

    work_dir = str(APP_DIR)
    instructions = load_instructions(work_dir)

    # 权限：dontAsk 让 Bash / WriteFile 直接放行（这是无人值守跑法）。
    # 交付护栏不靠权限模式 —— 它靠 delivery.guard_branch（只放行 agent/ 前缀）
    # 和 CreatePR 里硬编码的「验证先于交付」，以及配置里的 pre_tool_use hook。
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DONT_ASK,
    )

    registry = create_default_registry()
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        hook_engine=hook_engine,
    )

    wt_manager = WorktreeManager(repo_root=work_dir)
    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(work_dir, enable_verification=config.enable_verification_agent)
    agent_loader.load_all()
    team_manager = TeamManager(worktree_manager=wt_manager, trace_manager=trace_manager)

    registry.register(AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=config.enable_fork,
        provider_config=provider,
        worktree_manager=wt_manager,
        team_manager=team_manager,
    ))
    registry.register(TeamCreateTool(
        team_manager=team_manager,
        parent_agent=agent,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ))
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))

    assembly = assemble_toolset(registry, config.toolset, agent=agent, work_dir=work_dir)
    if assembly.prompt_note:
        agent.instructions_content = f"{instructions}\n\n{assembly.prompt_note}"

    print(f"[harness] 工具数={len(registry.list_tools())} "
          f"names={[t.name for t in registry.list_tools()]}", flush=True)
    print(f"[harness] work_dir={work_dir}", flush=True)
    print(f"[harness] 模型={provider.model} 权限模式={checker.mode.value}", flush=True)
    print(f"[harness] 运维数据源={type(assembly.ops).__name__}", flush=True)
    print("[harness] 开始排查…\n", flush=True)

    class _Echo:
        """把 agent 的事件打到 stdout，方便观察它排查到哪一步。

        `event_callback` 收到的是 dict（不是 StreamEvent 对象），三种类型：
        usage / stream_text / tool_use。
        """

        def __init__(self) -> None:
            self.tool_calls = 0

        def __call__(self, event: dict) -> None:
            kind = event.get("type")
            if kind == "tool_use":
                self.tool_calls += 1
                name = event.get("toolName", "?")
                args = event.get("args") or {}
                brief = ", ".join(f"{k}={str(v)[:70]}" for k, v in args.items())
                print(f"  ▸ [{self.tool_calls:02d}] {name}  {brief}", flush=True)
            elif kind == "usage":
                u = event.get("usage") or {}
                print(
                    f"      tokens in={u.get('inputTokens')} out={u.get('outputTokens')}",
                    flush=True,
                )

    result = await agent.run_to_completion(
        build_prompt(), ConversationManager(), event_callback=_Echo()
    )
    print("\n" + "=" * 78)
    print("最终回复")
    print("=" * 78)
    print(result)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印提示词")
    args = ap.parse_args()

    os.environ.setdefault("POD_NAME", "pod-3")
    if args.dry_run:
        print(build_prompt())
        return

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

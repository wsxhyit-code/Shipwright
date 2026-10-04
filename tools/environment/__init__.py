# 环境管理工具：让 agent 能「自己起测试环境」，但**只通过平台给定的命令**。
#
# ## 为什么不是「给它 docker 权限」
#
# 方向二的原始描述是「它跑在 Docker 容器里，自己起测试环境验证」。
# 但照字面实现会**打破它自己的沙箱**：
#
#     起容器需要 docker 权限；
#     给了 docker 权限 = 挂 docker.sock；
#     挂了 docker.sock，agent 就能 `docker run --privileged -v /:/host`
#     拿到宿主 root —— 这是业界最常见的沙箱穿透。
#
# 所以这个模块走的是另一条路：
#
#     **能力受限，但目标是达成的** —— agent 能起环境，
#     但只能执行平台**预先定义好的那几条命令**，不能自己拼 docker 命令。
#
# 平台在配置里写：
#
#     toolset:
#       environment:
#         start: "bash /app/scripts/up-test-env.sh"
#         stop:  "bash /app/scripts/down-test-env.sh"
#         status: "bash /app/scripts/env-status.sh"
#         reset: "bash /app/scripts/reset-test-db.sh"
#
# 容器里没有 docker、没有网络出口，所以 `up-test-env.sh` 只能去连
# 平台已经备好的东西 —— 这正是「平台造环境，agent 用环境」的边界，
# 只是把**启动动作**也交给 agent 触发。
#
# ## 两条硬规则
#
# **① 不配置就不注册工具。** 和 ops / knowledge 一致，不改变默认行为。
#
# **② 危险动作需要显式开启。** `stop` / `reset` 会破坏环境状态，
# 所以它们各自有一个 `allow_*` 开关，默认关闭 —— 并且默认值写在配置里
# 而不是靠提示词劝阻。`reset` 尤其危险：它可能清掉别人正在用的数据。
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

#: 单条环境命令的默认超时（起环境通常比跑测试慢）
DEFAULT_TIMEOUT = 600

#: 捕获输出的尾部长度
_TAIL = 4000


def split_command(cmd: str) -> list[str]:
    """拆命令。与 create_pr / delivery 同样的引号处理。

    Windows 上 `shlex.split(posix=False)` 会把引号保留在 token 里，
    于是 `"C:\\Program Files\\python.exe"` 会带引号执行、退出码 127。
    """
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return [cmd]
    out = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        out.append(t)
    return out or [cmd]


@dataclass
class _RunOutcome:
    action: str
    command: str
    ok: bool
    exit_code: int
    output: str
    elapsed: float
    denied_reason: str = ""


def _norm(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """把配置归一成一份 dict，缺的键补默认值。

    **刻意收 dict 而不是收 config 里的 dataclass** —— 和
    `tools/ops/backends/factory.py` 同一个模式：配置形状归 `config.py`，
    工具层不反向依赖 config，否则 `config → tools → config` 会循环导入。
    """
    cfg = cfg or {}
    out = {
        "start": "", "stop": "", "status": "", "reset": "",
        "allow_stop": False, "allow_reset": False,
        "timeout": DEFAULT_TIMEOUT, "settle_seconds": 0,
    }
    for k in out:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    return out


def describe_environment(cfg: Mapping[str, Any] | None) -> str:
    """给模型看的环境能力清单。没配任何命令时返回空串。"""
    c = _norm(cfg)
    acts = {k: c[k] for k in ("start", "stop", "status", "reset") if c[k]}
    if not acts:
        return ""
    lines = ["环境管理：平台提供了以下命令（你只能执行这几条）"]
    for k, v in acts.items():
        gate = ""
        if k == "stop" and not c["allow_stop"]:
            gate = "   ⚠️ 当前**禁止调用**（allow_stop 没开）"
        if k == "reset" and not c["allow_reset"]:
            gate = "   ⚠️ 当前**禁止调用**（allow_reset 没开）"
        lines.append(f"  · {k:<7} {v}{gate}")
    lines.append(
        "  容器里没有 docker / kubectl，所以起环境只能靠上面这几条命令，"
        "不要试着自己拼容器命令。"
    )
    return "\n".join(lines)


class EnvironmentRunner:
    """执行平台给定的环境命令。**工具层与测试都只依赖它。**"""

    def __init__(self, config: Mapping[str, Any] | None, work_dir: str = ".") -> None:
        self._cfg = _norm(config)
        self._work_dir = work_dir

    def _actions(self) -> dict[str, str]:
        return {k: self._cfg[k] for k in ("start", "stop", "status", "reset")
                if self._cfg[k]}

    def run(self, action: str, timeout: int | None = None) -> _RunOutcome:
        command = self._actions().get(action, "")
        if not command:
            available = ", ".join(self._actions()) or "（一个都没配）"
            return _RunOutcome(
                action, "", False, 1, "", 0.0,
                denied_reason=(
                    f"平台没有提供 `{action}` 这个动作。可用的动作：{available}。\n"
                    "不要尝试用别的方式起环境（容器里没有 docker/kubectl）。"
                ),
            )

        # 破坏性动作的门禁 —— 在**这里**拦，不靠提示词
        if action == "stop" and not self._cfg["allow_stop"]:
            return _RunOutcome(
                action, command, False, 1, "", 0.0,
                denied_reason=(
                    "`stop` 会停掉环境，当前配置里 allow_stop 未开启，拒绝执行。\n"
                    "如果需要停环境，请让人把 toolset.environment.allow_stop 打开，"
                    "或者人工执行这条命令。"
                ),
            )
        if action == "reset" and not self._cfg["allow_reset"]:
            return _RunOutcome(
                action, command, False, 1, "", 0.0,
                denied_reason=(
                    "`reset` 会重置环境数据（可能清掉别人正在用的东西），"
                    "当前配置里 allow_reset 未开启，拒绝执行。\n"
                    "如果需要重置，请明确让人开启该开关。"
                ),
            )

        t0 = time.monotonic()
        to = int(timeout or self._cfg["timeout"])
        try:
            p = subprocess.run(
                split_command(command),
                cwd=self._work_dir,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=to,
            )
        except subprocess.TimeoutExpired:
            return _RunOutcome(
                action, command, False, 124,
                f"超时（{to}s）：{command}", time.monotonic() - t0,
            )
        except FileNotFoundError as e:
            return _RunOutcome(
                action, command, False, 127,
                f"命令不存在：{e}\n（配置里的 environment.{action} 指向了一个"
                "当前环境里不存在的程序？）",
                time.monotonic() - t0,
            )

        out = (p.stdout or "") + (p.stderr or "")
        elapsed = time.monotonic() - t0

        # 有些平台脚本起完服务需要一点时间才 ready
        if p.returncode == 0 and self._cfg["settle_seconds"] > 0:
            time.sleep(self._cfg["settle_seconds"])

        return _RunOutcome(action, command, p.returncode == 0, p.returncode,
                           out, elapsed)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class _EnvTool(Tool):
    def __init__(self, runner: EnvironmentRunner | None) -> None:
        self._runner = runner

    def _require(self) -> EnvironmentRunner | str:
        if self._runner is None:
            return (
                "环境管理没有接入：平台没有在配置里提供任何环境命令。\n"
                "**这不代表「环境不需要起」** —— 只是没有接入。\n"
                "依赖服务通常已由平台预先启动，连接串通过环境变量注入；"
                "如果缺环境，请停下来报告，不要自己猜连接串。"
            )
        return self._runner

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def _render(self, o: _RunOutcome) -> ToolResult:
        if o.denied_reason:
            return ToolResult(output=f"❌ {o.denied_reason}", is_error=True)
        verb = "✅" if o.ok else "❌"
        head = (
            f"{verb} environment.{o.action} → 退出码 {o.exit_code}"
            f"（{o.elapsed:.1f}s）\n命令：{o.command}"
        )
        if o.ok:
            head += (
                "\n（这是平台给定的命令，它已经按约定把环境准备好了）"
            )
        else:
            head += (
                "\n\n**环境没起来，后续步骤不会可靠。** "
                "不要在这种情况下继续跑测试并相信结果 —— "
                "先把这条错误报告出来，或者按平台的说明排查。"
            )
        body = o.output[-_TAIL:] if o.output else "（无输出）"
        return ToolResult(output=f"{head}\n\n输出（尾部 {_TAIL} 字符）：\n{body}",
                          is_error=not o.ok)


class StartTestEnvParams(BaseModel):
    timeout: int = Field(default=0, description="超时秒数；0 表示用配置里的默认值")


class StartTestEnvTool(_EnvTool):
    name = "StartTestEnv"
    description = (
        "启动测试环境（数据库 / Redis / mock 服务等）。\n"
        "它执行的是**平台预先配置好的那一条命令**，不是你拼出来的 ——"
        "容器里没有 docker/kubectl，也起不了容器。\n"
        "起完环境再跑测试。如果这条命令失败，**不要继续跑测试并相信结果**。"
    )
    params_model = StartTestEnvParams
    category = "command"
    is_concurrency_safe = False

    async def execute(self, params: BaseModel) -> ToolResult:
        p: StartTestEnvParams = params  # type: ignore[assignment]
        r = self._require()
        if isinstance(r, str):
            return ToolResult(output=f"❌ {r}", is_error=True)
        return self._render(r.run("start", p.timeout or None))


class StopTestEnvParams(BaseModel):
    timeout: int = Field(default=0, description="超时秒数；0 表示用配置里的默认值")


class StopTestEnvTool(_EnvTool):
    name = "StopTestEnv"
    description = (
        "停止测试环境。**这是破坏性动作**，会中断环境里正在跑的东西。\n"
        "通常不需要你来停 —— 任务结束由平台回收。\n"
        "当前配置若未开启 allow_stop，调用会被直接拒绝。"
    )
    params_model = StopTestEnvParams
    category = "command"
    is_concurrency_safe = False

    async def execute(self, params: BaseModel) -> ToolResult:
        p: StopTestEnvParams = params  # type: ignore[assignment]
        r = self._require()
        if isinstance(r, str):
            return ToolResult(output=f"❌ {r}", is_error=True)
        return self._render(r.run("stop", p.timeout or None))


class ResetTestEnvParams(BaseModel):
    timeout: int = Field(default=0, description="超时秒数；0 表示用配置里的默认值")


class ResetTestEnvTool(_EnvTool):
    name = "ResetTestEnv"
    description = (
        "重置测试环境的数据（例如重建数据库、重跑迁移）。\n"
        "**这是破坏性动作**：会清掉环境里的现有数据，可能影响别人正在用的实例。\n"
        "当前配置若未开启 allow_reset，调用会被直接拒绝 —— 那就不要绕路。"
    )
    params_model = ResetTestEnvParams
    category = "command"
    is_concurrency_safe = False

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ResetTestEnvParams = params  # type: ignore[assignment]
        r = self._require()
        if isinstance(r, str):
            return ToolResult(output=f"❌ {r}", is_error=True)
        return self._render(r.run("reset", p.timeout or None))


class EnvStatusParams(BaseModel):
    pass


class EnvStatusTool(_EnvTool):
    name = "EnvStatus"
    description = (
        "查看测试环境当前状态（只读）。\n"
        "起环境前先看一眼能避免重复启动；测试莫名失败时也先看它 ——"
        "很多『测试挂了』其实是环境没起来。"
    )
    params_model = EnvStatusParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        r = self._require()
        if isinstance(r, str):
            return ToolResult(output=f"❌ {r}", is_error=True)
        return self._render(r.run("status"))


ENV_TOOLS: tuple[type[_EnvTool], ...] = (
    StartTestEnvTool,
    StopTestEnvTool,
    ResetTestEnvTool,
    EnvStatusTool,
)


def _actions_of(cfg: Mapping[str, Any] | None) -> dict[str, str]:
    c = _norm(cfg)
    return {k: c[k] for k in ("start", "stop", "status", "reset") if c[k]}


def register_env_tools(registry: Any, config: Mapping[str, Any] | None,
                       work_dir: str = ".") -> list[str]:
    """注册环境工具。**没配置任何命令时返回空列表（不注册）。**

    和 ops / knowledge / CreatePR 一致：不配置的人工具集和以前完全一样。
    """
    if not _actions_of(config):
        return []
    runner = EnvironmentRunner(config, work_dir)
    for cls in ENV_TOOLS:
        registry.register(cls(runner))
    return [cls.name for cls in ENV_TOOLS]


__all__ = [
    "DEFAULT_TIMEOUT",
    "ENV_TOOLS",
    "EnvStatusTool",
    "EnvironmentRunner",
    "ResetTestEnvTool",
    "StartTestEnvTool",
    "StopTestEnvTool",
    "describe_environment",
    "register_env_tools",
    "split_command",
]

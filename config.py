from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_TEAMMATE_MODES,
    lookup_model_context_window,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        env_var = _ENV_KEY_MAP.get(self.protocol, "")
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000
        return 8192


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    #: 该 server 的全部工具套用哪个权限类别：read / write / command。
    #: 默认 command（保持旧行为）。只读的运维 server（日志、指标、部署查询）
    #: 应该配 `read` —— 否则每次查询都会弹窗，排查故障根本没法连查。
    category: str = "command"


    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class WorktreeConfig:
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", ".venv", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class OpsBackendConfig:
    """一个运维后端。`capability` 决定它接哪个方法（logs / metrics / alerts…）。"""

    kind: str                      # mock | loki | prometheus | alertmanager
    capability: str                # alerts | logs | metrics | deploys | health | incidents
    base_url: str = ""
    token_env: str = ""            # 推荐：从环境变量读 token，配置里不落密钥
    token: str = ""                # 直接写死（不推荐，会进版本库）
    timeout: float = 15.0


@dataclass
class CreatePRConfig:
    """CreatePR 门禁的配置。

    `mode` 决定 agent 自己走到哪一步：

        patch  只产出补丁（容器模式：agent 连不上远端，由 CI 收尾推送）
        push   自己提交并推 `agent/*` 分支
        pr     再开 PR（需要 gh 或 GITHUB_TOKEN）

    默认 `patch` 是最保守的一档，行为和加这个开关之前完全一致。
    三种模式**都**遵守同样的护栏：绝不推基线分支、绝不合并、
    验证必须在推送之前完成。
    """

    enabled: bool = False
    verify_command: str = ""
    artifacts_dir: str = ".mewcode/pr"
    base_ref: str = "main"
    require_independent: bool = True
    timeout: float = 900.0
    mode: str = "patch"
    remote: str = "origin"
    api_base: str = ""


@dataclass
class ToolsetConfig:
    """把运维后端和 CreatePR 接进运行中的 agent。

    没有这一段的话，`tools/ops/` 和 `tools/create_pr.py` 就只是"能通过测试的
    代码"，而不是 agent 真的有的能力 —— 它们不会出现在任何一次运行的
    registry 里。
    """

    ops: list[OpsBackendConfig] = field(default_factory=list)
    create_pr: CreatePRConfig = field(default_factory=CreatePRConfig)


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str = "default"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)
    enable_fork: bool = False
    enable_verification_agent: bool = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    enable_coordinator_mode: bool = False
    toolset: ToolsetConfig = field(default_factory=ToolsetConfig)


def _load_single_file(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    validated = validate_config_structure(raw)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
            category=s.get("category", "command"),
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    ts = validated["toolset"]
    cp = ts["create_pr"]
    toolset_cfg = ToolsetConfig(
        ops=[
            OpsBackendConfig(
                kind=o["kind"],
                capability=o["capability"],
                base_url=o["base_url"],
                token_env=o["token_env"],
                token=o["token"],
                timeout=o["timeout"],
            )
            for o in ts["ops"]
        ],
        create_pr=CreatePRConfig(
            enabled=cp["enabled"],
            verify_command=cp["verify_command"],
            artifacts_dir=cp["artifacts_dir"],
            base_ref=cp["base_ref"],
            require_independent=cp["require_independent"],
            timeout=cp["timeout"],
            mode=cp["mode"],
            remote=cp["remote"],
            api_base=cp["api_base"],
        ),
    )

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        toolset=toolset_cfg,
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    if override.providers:
        base.providers = override.providers
    if override.permission_mode != "default":
        base.permission_mode = override.permission_mode

    if override.mcp_servers:
        by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
        for s in override.mcp_servers:
            if s.name in by_name:
                base.mcp_servers[by_name[s.name]] = s
            else:
                base.mcp_servers.append(s)
                by_name[s.name] = len(base.mcp_servers) - 1

    base.raw_hooks.extend(override.raw_hooks)
    if override.enable_fork:
        base.enable_fork = True
    if override.enable_verification_agent:
        base.enable_verification_agent = True
    if override.teammate_mode:
        base.teammate_mode = override.teammate_mode
    if override.enable_coordinator_mode:
        base.enable_coordinator_mode = True

    # toolset 的合并：ops 按 capability 覆盖（覆盖层里写了同能力的就顶掉），
    # create_pr 一旦在覆盖层出现就整体替换 —— 它是"要么开要么不开"的开关，
    # 逐字段合并会让"我想关掉它"这件事没法表达。
    if override.toolset.ops:
        by_cap = {o.capability: i for i, o in enumerate(base.toolset.ops)}
        for o in override.toolset.ops:
            if o.capability in by_cap:
                base.toolset.ops[by_cap[o.capability]] = o
            else:
                base.toolset.ops.append(o)
                by_cap[o.capability] = len(base.toolset.ops) - 1
    if override.toolset.create_pr.enabled or override.toolset.create_pr.verify_command:
        base.toolset.create_pr = override.toolset.create_pr

    return base


def _check_toolset_env(cfg: AppConfig) -> None:
    """检查 toolset.ops 里声明的 token 环境变量在不在。

    放在**配置加载阶段**而不是等构造后端时，有两个好处：

      1. 报错走的是 `ConfigError` 这条已经有清晰提示的路径（`__main__.py` 会接住）
      2. 问题在启动时就暴露，而不是等到半夜排查故障、
         满怀信心地向 Loki 发请求、然后拿到一个 401

    缺失就报错而不是"跳过这个后端"：静默少一个数据源，
    在故障排查里会被读成"那方面没有问题" —— 正是 `OpsCapabilityMissing`
    要防的那个错误，只不过更隐蔽。
    """
    missing: list[str] = []
    for i, ops in enumerate(cfg.toolset.ops):
        if not ops.token_env:
            continue
        if not (os.environ.get(ops.token_env) or "").strip():
            missing.append(
                f"  toolset.ops[{i}] (kind={ops.kind}, capability={ops.capability}) "
                f"需要环境变量 {ops.token_env}，但它没有设置或为空"
            )
    if missing:
        raise ConfigError(
            "运维后端的凭据缺失：\n"
            + "\n".join(missing)
            + "\n请在启动前导出这些环境变量，例如：export LOKI_READONLY_TOKEN=<token>\n"
            "（如果这个后端允许匿名访问，请把配置里对应的 token_env 那一行删掉，"
            "而不是留一个读不到的环境变量名。）"
        )


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        single = _load_single_file(path)
        # 显式指定路径时没有合并步骤，这里就是最终结果，必须自带 provider
        if not single.providers:
            raise ConfigError(f"{path} must contain a 'providers' list")
        _check_toolset_env(single)
        return single

    cwd = Path.cwd()
    home = Path.home()
    candidates = [
        home / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.local.yaml",
    ]

    merged: AppConfig | None = None
    for p in candidates:
        if not p.exists():
            continue
        layer = _load_single_file(p)
        if merged is None:
            merged = layer
        else:
            merged = _merge_config(merged, layer)

    if merged is None:
        raise ConfigError(
            "No config file found. Expected .mewcode/config.yaml "
            "in project or ~/.mewcode/config.yaml"
        )
    # providers 的存在性在**合并完成后**检查，而不是在每个文件上检查。
    # 否则 config.local.yaml 这种"只覆盖 hooks"的层必须把 provider 配置抄一遍。
    if not merged.providers:
        raise ConfigError(
            "No providers configured. Add a 'providers' list to "
            ".mewcode/config.yaml"
        )
    _check_toolset_env(merged)
    return merged

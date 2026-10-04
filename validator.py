"""Shipwright 的配置校验逻辑。"""

from __future__ import annotations

import os
import shutil

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat"}

#: 工具权限类别，和 tools/base.py 的 ToolCategory 对齐。
#: MCP server 可以为它下面所有工具声明一个类别。
VALID_TOOL_CATEGORIES = {"read", "write", "command"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "custom",
    "dontAsk",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

DEFAULT_CONTEXT_WINDOW = 200_000

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


class ConfigError(Exception):
    pass


def validate_providers(raw_providers: list | None, allow_empty: bool = False) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。

    `allow_empty=True` 用于**覆盖层**（如 `.mewcode/config.local.yaml`）：
    这类文件只覆盖 hooks / mcp_servers，不该被要求把 provider 配置抄一遍。
    "最终必须至少有一个 provider" 由 `load_config` 在合并完成后检查。
    """
    if raw_providers is None:
        if allow_empty:
            return []
        raise ConfigError("At least one provider must be configured")
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        if allow_empty:
            return []
        raise ConfigError("At least one provider must be configured")

    providers: list[dict] = []
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"Provider #{i + 1}: missing fields: {', '.join(missing)}")

        protocol = entry["protocol"]
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"Provider #{i + 1}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"Provider #{i + 1}: context_window must be a positive integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"Provider #{i + 1}: thinking must be a boolean")

        max_output_tokens = entry.get("max_output_tokens", 0)
        if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise ConfigError(
                f"Provider #{i + 1}: max_output_tokens must be a non-negative integer"
            )

        providers.append(
            {
                "name": entry["name"],
                "protocol": protocol,
                "base_url": entry["base_url"],
                "model": entry["model"],
                "api_key": entry.get("api_key", ""),
                "thinking": thinking,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    return providers


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


def validate_mcp_servers(raw_mcp: list | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        name = entry.get("name")
        if not name:
            raise ConfigError(f"MCP server #{i + 1}: missing 'name'")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        category = entry.get("category", "command")
        if category not in VALID_TOOL_CATEGORIES:
            raise ConfigError(
                f"MCP server '{name}': invalid category '{category}', "
                f"must be one of: {', '.join(sorted(VALID_TOOL_CATEGORIES))}"
            )
        servers.append(
            {
                "name": name,
                "command": entry.get("command"),
                "args": entry.get("args", []),
                "url": entry.get("url"),
                "headers": entry.get("headers", {}),
                "env": entry.get("env", {}),
                "category": category,
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    return raw_hooks


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", ".venv", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


# ---------------------------------------------------------------------------
# toolset：把运维后端和 CreatePR 接进运行中的 agent
# ---------------------------------------------------------------------------

VALID_OPS_KINDS = {"mock", "loki", "prometheus", "alertmanager"}

VALID_OPS_CAPABILITIES = {"alerts", "logs", "metrics", "deploys", "health", "incidents"}

#: 每种后端默认提供哪个能力。可以显式写 capability 覆盖
#: （比如同一个 Loki 地址挂两个后端，一个查日志一个查别的）。
_DEFAULT_OPS_CAPABILITY = {
    "loki": "logs",
    "prometheus": "metrics",
    "alertmanager": "alerts",
    "mock": "mock",
}

VALID_CREATE_PR_MODES = {"patch", "push", "pr"}

VALID_KNOWLEDGE_KINDS = {"local", "http", "mock"}

DEFAULT_KNOWLEDGE = {
    "kind": "local",
    "extra_dirs": [],
    "include_user_dir": True,
    "base_url": "",
    "path": "/search",
    "query_param": "q",
    "limit_param": "limit",
    "read_path": "",
    "read_id_field": "content",
    "token_env": "",
    "token": "",
    "timeout": 15.0,
    "trust_env": False,
    "mapping": {},
    "name": "",
}

DEFAULT_ENVIRONMENT = {
    "start": "",
    "stop": "",
    "status": "",
    "reset": "",
    "allow_stop": False,
    "allow_reset": False,
    "timeout": 600,
    "settle_seconds": 0,
}


def validate_environment(raw: object) -> dict | None:
    """校验 `toolset.environment` —— 平台给定的环境命令。

    缺省返回 None（= 不接，agent 只连环境不起环境）。

    ⚠️ `allow_stop` / `allow_reset` **默认关**。
    `reset` 尤其危险：它可能清掉别人正在用的数据。
    所以这两个开关在**代码里**拦（`EnvironmentRunner.run`），
    不靠提示词劝阻 —— 提示词是可以被绕过的。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("toolset.environment must be a mapping")

    out = dict(DEFAULT_ENVIRONMENT)
    for key in ("start", "stop", "status", "reset"):
        if key in raw:
            v = raw[key]
            if not isinstance(v, str):
                raise ConfigError(f"toolset.environment.{key} must be a string")
            out[key] = v.strip()

    if not any(out[k] for k in ("start", "stop", "status", "reset")):
        raise ConfigError(
            "toolset.environment 配了但一条命令都没有。\n"
            "至少要给 start（起环境）或 status（查状态），否则这一段没有意义 —— "
            "直接删掉它，或者补上命令，例如：\n"
            "  environment:\n"
            "    start: \"bash /app/scripts/up-test-env.sh\""
        )

    out["allow_stop"] = validate_bool_field(
        raw.get("allow_stop", False), "toolset.environment.allow_stop"
    )
    out["allow_reset"] = validate_bool_field(
        raw.get("allow_reset", False), "toolset.environment.allow_reset"
    )
    for key, floor in (("timeout", 1), ("settle_seconds", 0)):
        if key in raw:
            v = raw[key]
            if not isinstance(v, (int, float)) or v < floor:
                raise ConfigError(
                    f"toolset.environment.{key} must be a number >= {floor}"
                )
            out[key] = int(v)

    # 开了破坏性开关要能看见 —— 写进日志式的提示里，不是静默接受
    if out["allow_reset"] and not out["reset"]:
        raise ConfigError(
            "toolset.environment.allow_reset 开着，但没配 reset 命令"
        )
    if out["allow_stop"] and not out["stop"]:
        raise ConfigError("toolset.environment.allow_stop 开着，但没配 stop 命令")
    return out


DEFAULT_CREATE_PR = {
    "enabled": False,
    "verify_command": "",
    "artifacts_dir": ".mewcode/pr",
    "base_ref": "main",
    "require_independent": True,
    "timeout": 900,
    "mode": "patch",
    "remote": "origin",
    "api_base": "",
    "standards": [],
}


def validate_knowledge(raw: object) -> dict | None:
    """校验 `toolset.knowledge`。

    缺省返回 None（= 不接知识库）。这**不是**错误配置，
    但工具仍然会注册 —— 调用时抛 `KnowledgeUnavailable`，
    这样模型能明确知道「没接入」，而不是把空结果读成「内部没有规定」。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("toolset.knowledge must be a mapping")

    out = dict(DEFAULT_KNOWLEDGE)
    out["mapping"] = {}

    kind = raw.get("kind", "local")
    if kind not in VALID_KNOWLEDGE_KINDS:
        raise ConfigError(
            f"toolset.knowledge.kind must be one of: "
            f"{', '.join(sorted(VALID_KNOWLEDGE_KINDS))}（收到 {kind!r}）"
        )
    out["kind"] = kind

    if kind == "http" and not str(raw.get("base_url") or "").strip():
        raise ConfigError(
            "toolset.knowledge.kind = 'http' 但没配 base_url。\n"
            "接内部检索 API 必须给地址，例如：\n"
            "  base_url: \"https://kb.internal/api\""
        )

    for key in ("base_url", "path", "query_param", "limit_param",
                "read_path", "read_id_field", "token_env", "token", "name"):
        if key in raw:
            v = raw[key]
            if not isinstance(v, str):
                raise ConfigError(f"toolset.knowledge.{key} must be a string")
            out[key] = v.strip()

    if "extra_dirs" in raw:
        v = raw["extra_dirs"]
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ConfigError("toolset.knowledge.extra_dirs must be a list of strings")
        out["extra_dirs"] = [x.strip() for x in v if x.strip()]

    out["include_user_dir"] = validate_bool_field(
        raw.get("include_user_dir", True), "toolset.knowledge.include_user_dir"
    )
    out["trust_env"] = validate_bool_field(
        raw.get("trust_env", False), "toolset.knowledge.trust_env"
    )

    if "timeout" in raw:
        t = raw["timeout"]
        if not isinstance(t, (int, float)) or t <= 0:
            raise ConfigError("toolset.knowledge.timeout must be a positive number")
        out["timeout"] = float(t)

    if "mapping" in raw:
        m = raw["mapping"]
        if not isinstance(m, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in m.items()
        ):
            raise ConfigError(
                "toolset.knowledge.mapping must be a mapping of string -> string，"
                "例如 response_path: 'hits.hits'"
            )
        out["mapping"] = dict(m)

    return out


def validate_standards(raw: object) -> list[dict]:
    """校验 `toolset.create_pr.standards` —— 规范校验关卡。

    每条形如 `{name, command, hint}`，`command` 退出码非 0 即不通过。
    这是把「代码符合内部标准」从提示词软约束变成硬关卡的地方，
    所以校验从严：名字和命令都不能空。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("toolset.create_pr.standards must be a list")

    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        label = f"toolset.create_pr.standards[{i}]"
        if isinstance(item, str):
            item = {"name": item, "command": item}
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be a mapping（或一个字符串命令）")

        name = item.get("name")
        command = item.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{label}: 缺少 name（用来在报告里区分是哪条检查）")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"{label} ({name}): 缺少 command")
        if name in seen:
            raise ConfigError(f"{label}: name {name!r} 重复了")
        seen.add(name)

        hint = item.get("hint", "")
        if not isinstance(hint, str):
            raise ConfigError(f"{label} ({name}): hint must be a string")

        out.append({"name": name.strip(), "command": command.strip(), "hint": hint.strip()})
    return out


def validate_ops_backends(raw: object) -> list[dict]:
    """校验 `toolset.ops` 列表。

    这里的校验刻意做得**严**：地址写错、kind 拼错、token 环境变量名打错，
    都应该在启动时就炸。理由是这个配置只有在**真出事的时候**才会被用到 ——
    那时候才发现"原来配置根本没生效"，代价是一次故障排查。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("toolset.ops must be a list")

    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        label = f"toolset.ops[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be a mapping")

        kind = item.get("kind")
        if kind not in VALID_OPS_KINDS:
            raise ConfigError(
                f"{label}: unknown kind {kind!r}, "
                f"must be one of: {', '.join(sorted(VALID_OPS_KINDS))}"
            )

        # mock 是个**完整场景对象**（八个方法都有），不按能力拆分，
        # 所以它的 capability 固定为 "mock"，不参与能力白名单校验。
        # 它也不能和别的后端混用 —— 那条规则在 factory 里强制
        # （那里才是真正决定怎么拼的地方）。
        if kind == "mock":
            capability = "mock"
        else:
            capability = item.get("capability") or _DEFAULT_OPS_CAPABILITY[kind]
            if capability not in VALID_OPS_CAPABILITIES:
                raise ConfigError(
                    f"{label}: unknown capability {capability!r}, "
                    f"must be one of: {', '.join(sorted(VALID_OPS_CAPABILITIES))}"
                )
            if capability in seen:
                raise ConfigError(
                    f"{label}: capability {capability!r} 已经配过了 —— "
                    "每个能力只能有一个后端（CompositeOpsBackend 按能力一对一路由）"
                )
            seen.add(capability)

        base_url = item.get("base_url", "") or ""
        if not isinstance(base_url, str):
            raise ConfigError(f"{label}: base_url must be a string")
        if kind != "mock" and not base_url:
            raise ConfigError(f"{label}: kind={kind} 需要 base_url")
        if kind == "mock" and base_url:
            raise ConfigError(f"{label}: kind=mock 不需要 base_url")

        token_env = item.get("token_env", "") or ""
        token = item.get("token", "") or ""
        if not isinstance(token_env, str) or not isinstance(token, str):
            raise ConfigError(f"{label}: token_env / token must be strings")
        if token and not token_env:
            # 允许，但要在文档里说清风险：token 落在配置文件里会进版本库
            pass

        timeout = item.get("timeout", 15.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"{label}: timeout must be a positive number")

        out.append(
            {
                "kind": kind,
                "capability": capability,
                "base_url": base_url,
                "token_env": token_env,
                "token": token,
                "timeout": float(timeout),
            }
        )
    return out


def validate_toolset(raw: object) -> dict:
    """校验 `toolset` 段。缺省时返回"什么都不接"。

    `create_pr.enabled` 为真却**没有 verify_command** 时直接报错。这是刻意的：
    没有验证命令的 CreatePR 等于把"AI 自己验证通过才提 PR"这条铁律拆掉了，
    而它看起来还能正常工作 —— 这种"悄悄降级"比启动失败危险得多。
    """
    if raw is None:
        return {
            "ops": [],
            "create_pr": dict(DEFAULT_CREATE_PR),
            "knowledge": None,
            "plugins": False,
            "environment": None,
        }
    if not isinstance(raw, dict):
        raise ConfigError("toolset must be a mapping")

    cp_raw = raw.get("create_pr") or {}
    if not isinstance(cp_raw, dict):
        raise ConfigError("toolset.create_pr must be a mapping")

    cp = dict(DEFAULT_CREATE_PR)
    cp["enabled"] = validate_bool_field(
        cp_raw.get("enabled", False), "toolset.create_pr.enabled"
    )
    for key in ("verify_command", "artifacts_dir", "base_ref"):
        if key in cp_raw:
            value = cp_raw[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"toolset.create_pr.{key} must be a non-empty string")
            cp[key] = value.strip()
    cp["require_independent"] = validate_bool_field(
        cp_raw.get("require_independent", True),
        "toolset.create_pr.require_independent",
    )
    if "timeout" in cp_raw:
        t = cp_raw["timeout"]
        if not isinstance(t, (int, float)) or t <= 0:
            raise ConfigError("toolset.create_pr.timeout must be a positive number")
        cp["timeout"] = float(t)

    # mode / remote / api_base
    if "mode" in cp_raw:
        mode = cp_raw["mode"]
        if mode not in VALID_CREATE_PR_MODES:
            raise ConfigError(
                f"toolset.create_pr.mode must be one of: "
                f"{', '.join(sorted(VALID_CREATE_PR_MODES))}（收到 {mode!r}）"
            )
        cp["mode"] = mode
    for key in ("remote", "api_base"):
        if key in cp_raw:
            value = cp_raw[key]
            if not isinstance(value, str):
                raise ConfigError(f"toolset.create_pr.{key} must be a string")
            cp[key] = value.strip()

    cp["standards"] = validate_standards(cp_raw.get("standards"))

    # mode=pr 要有办法开 PR：gh 或 token，二者必须有其一。
    # 缺失时**启动就报错** —— 否则 agent 会在改完代码、验证通过、
    # 分支都推上去之后才发现开不了 PR，白跑一整轮。
    if cp["enabled"] and cp["mode"] == "pr":
        has_gh = bool(shutil.which("gh"))
        has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        if not (has_gh or has_token):
            raise ConfigError(
                "toolset.create_pr.mode = 'pr'，但本机既没有 `gh` 命令，"
                "也没有 GITHUB_TOKEN / GH_TOKEN —— 开不了 PR。\n"
                "Agent 会在改完代码、两层验证都通过、分支都推上去之后才发现这件事。\n"
                "请二选一：装 gh 并登录、或 export GITHUB_TOKEN=<有 repo 权限的 token>；\n"
                "或者把 mode 改成 'push'（只推分支，PR 由人或 CI 开）。"
            )

    if cp["enabled"] and not cp["verify_command"]:
        raise ConfigError(
            "toolset.create_pr.enabled 为 true 但没配 verify_command。\n"
            "CreatePR 的整个意义就是「验证不过就不产出补丁」，没有验证命令"
            "它就只能生成一个未经验证的补丁 —— 这比不开启更危险（看起来在工作）。\n"
            "请补上 verify_command，例如：verify_command: \"python -m pytest tests/ -q\""
        )

    return {
        "ops": validate_ops_backends(raw.get("ops")),
        "create_pr": cp,
        "knowledge": validate_knowledge(raw.get("knowledge")),
        "plugins": validate_bool_field(raw.get("plugins", False), "toolset.plugins"),
        "environment": validate_environment(raw.get("environment")),
    }


def validate_config_structure(raw: object) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode、toolset

    ⚠️ 修复记录：旧实现在**每个文件**上都要求 `providers`，于是
    `<proj>/.mewcode/config.local.yaml`（本来就在 load_config 的候选列表里）
    根本没法当"覆盖层"用 —— 想只覆盖 hooks 就必须把 provider 配置原样抄一遍，
    而抄完之后改了主配置又会被覆盖层顶回去。

    现在这一层允许 `providers` 缺省（视为 []），由 `load_config` 在**合并完成后**
    统一检查"最终有没有 provider"。这样它才真的能当一个覆盖层。
    """
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a mapping")

    return {
        # allow_empty：单个文件的校验不强制 providers，合并后再查
        "providers": validate_providers(raw.get("providers"), allow_empty=True),
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
        "toolset": validate_toolset(raw.get("toolset")),
    }

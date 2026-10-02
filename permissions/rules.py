from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

Effect = Literal["allow", "deny"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

# 数值比较语法：`CreatePO(amount > 10000)`。
# 这是给"金额/数量阈值"这类规则用的 —— fnmatch 字符串通配做不到数值比较，
# 而业务权限的核心往往恰恰是阈值（下单金额超过多少要走审批）。
_NUMERIC_RE = re.compile(
    r"^\s*([\w.]+)\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$"
)

# 每个工具的"内容字段"，**按重要性排序**：
#   第 1 个是主字段，用于生成规则模式（extract_content）
#   全部字段都会被路径沙箱逐个校验（extract_paths）
#
# ⚠️ 修复记录：旧实现把 Glob/Grep 映射到 "pattern"，但真正决定"搜哪里"的是
#    `path`（见 tools/glob.py:29 与 tools/grep.py:31 的 `base = Path(params.path)`）。
#    结果是 `Glob(pattern="*.ini", path="C:/Windows")` 只把 `*.ini` 送进沙箱，
#    而 `C:/Windows` 完全没被校验 —— 一条真实的只读越权路径。
#    现在两个字段都校验，且主字段改成真正指向文件系统的 `path`。
_CONTENT_FIELDS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "ReadFile": ("file_path",),
    "WriteFile": ("file_path",),
    "EditFile": ("file_path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path", "pattern"),
}

# 从 shell 命令里粗提绝对路径。
#   Windows: 盘符路径，但**前面不能是单词字符**——否则 `https:/` 里的 `s:/`
#            会被误判成盘符 `S:`（实测踩到过：`curl https://api...` 被提成
#            `s://api...`，导致正常网络命令被沙箱拒掉）
#   POSIX  : /x，排除 //（协议相对 URL）与紧跟在 ':' 后的 /（https://...）
# 这是启发式：挡不住变量拼接 / base64 等构造，但能挡住最常见的一类越权写。
_WIN_ABS_RE = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'|;&<>]*")
_POSIX_ABS_RE = re.compile(r"(?<![\w.:/-])/(?!/)[^\s\"'|;&<>]*")

# 这些"路径"是设备或标准流，不属于作用域问题，跳过
_DEVICE_PATHS = frozenset(
    {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/zero"}
)


@dataclass(frozen=True)
class Rule:
    """一条权限规则。

    两种形态，由 `op` 是否为空区分：

      · **通配规则**（`op == ""`）：`Bash(pytest -q*)` —— `fnmatch` 字符串匹配
      · **数值规则**：`CreatePO(amount > 10000)` —— 把 content 解析成数字再比较

    数值规则是这次补上的：`fnmatch` 只能做字符串通配，表达不了"金额超过多少"，
    而业务权限的核心恰恰常是阈值。`pattern` 字段在两种形态下都保存**可回显的
    原始表达式**（如 `amount > 10000`），这样写回 YAML 时能原样往返。
    """

    tool_name: str
    pattern: str
    effect: Effect
    #: 数值比较运算符；空串表示这是通配规则
    op: str = ""
    #: 数值阈值，仅在 op 非空时有意义
    threshold: float = 0.0


    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if not self.op:
            return fnmatch(content, self.pattern)
        try:
            value = float(str(content).strip())
        except (TypeError, ValueError):
            # 取不到数值 → 数值规则不匹配。
            # 刻意**不**降级成"匹配成功"，否则一个拼错的字段名会让规则变成全量放行。
            return False
        return _compare(value, self.op, self.threshold)


def _compare(value: float, op: str, threshold: float) -> bool:
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    return False


def parse_rule(raw: str, effect: Effect) -> Rule:
    """解析规则表达式。支持两种语法：

        Bash(pytest -q*)            → 通配规则
        CreatePO(amount > 10000)    → 数值规则
    """
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    tool_name, inner = m.group(1), m.group(2)

    num = _NUMERIC_RE.match(inner)
    if num is not None:
        field, op, threshold = num.group(1), num.group(2), float(num.group(3))
        # pattern 保存可回显的规范形式，保证写回 YAML 后还能解析回来
        return Rule(
            tool_name=tool_name,
            pattern=f"{field} {op} {threshold:g}",
            effect=effect,
            op=op,
            threshold=threshold,
        )

    return Rule(tool_name=tool_name, pattern=inner, effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    """取**主字段**的值。

    用途：生成 ALLOW_ALWAYS 的规则模式、危险命令检测。
    返回空串表示"这个工具没有可用的作用域字段"——调用方必须据此
    **拒绝生成规则**，否则 `f"{content}*"` 会退化成 `"*"`（全量永久放行）。
    """
    fields = _CONTENT_FIELDS.get(tool_name)
    if not fields:
        return ""
    return str(arguments.get(fields[0], ""))


def extract_paths(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    """取**所有可能指向文件系统的字段值**，供路径沙箱逐个校验。

    Glob/Grep 的修复点就在这里：只校验 pattern 而漏掉 path，等于没校验。
    """
    fields = _CONTENT_FIELDS.get(tool_name)
    if not fields:
        return []
    out: list[str] = []
    for f in fields:
        v = arguments.get(f, "")
        if v:
            out.append(str(v))
    return out


def extract_command_paths(command: str) -> list[str]:
    """从 shell 命令里粗提绝对路径，供沙箱校验。

    command 类工具（Bash / MCP）旧实现**完全不走沙箱**，因为 `checker.py`
    的 Layer 2 条件是 `category in ("read","write")`。这里把这条路补上。
    """
    if not command:
        return []
    found: list[str] = []
    for m in _WIN_ABS_RE.finditer(command):
        found.append(m.group(0))
    for m in _POSIX_ABS_RE.finditer(command):
        found.append(m.group(0))

    out: list[str] = []
    seen: set[str] = set()
    for p in found:
        p = p.rstrip(".,;:)\"'")
        if not p or p in seen or p in _DEVICE_PATHS:
            continue
        seen.add(p)
        out.append(p)
    return out


def iter_sandbox_targets(
    tool_name: str, category: str, arguments: dict[str, Any]
) -> list[str]:
    """汇总一次工具调用需要交给路径沙箱校验的全部候选值。"""
    if category in ("read", "write"):
        return extract_paths(tool_name, arguments)
    if category == "command":
        return extract_command_paths(str(arguments.get("command", "")))
    return []


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers


    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule.effect
        return None


    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [{"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect} for r in existing]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")

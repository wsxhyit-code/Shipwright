#!/usr/bin/env python3
"""内部规范检查器 —— 给 `toolset.create_pr.standards` 用的可执行关卡。

## 为什么要有这个文件

方向二要的是「agent 写出来的代码天然符合内部标准」。把规范写进
system prompt 只是**软约束**：模型可能没读、读漏、或者读懂了但写完就忘。

这个脚本把「内部标准」变成**可执行的检查**，接进 CreatePR 门禁：

    toolset:
      create_pr:
        standards:
          - name: 内部规范
            command: "python scripts/check_standards.py"

任何一条不过 → 退出码非 0 → **拒绝交付**。和「测试是否通过」一样是硬关卡。

## 它检查什么

这些检查都是「测试套件抓不到、但提交上去会出事」的类型：

  ① **硬编码凭据** —— 测试全过也不代表没把密钥写进代码
  ② **裸 TODO/FIXME** —— 要求带上 issue 编号，否则没人会回来处理
  ③ **文件末尾换行** —— POSIX 文本文件约定；缺了会让 diff 显示 `\\ No newline`

输出是给人（和模型）看的：每条失败都带上**文件、行号、原文**，
这样模型能直接改，而不是猜。

用法：
    python scripts/check_standards.py            # 检查整个仓库
    python scripts/check_standards.py path...    # 只检查指定路径
"""
from __future__ import annotations

import pathlib
import re
import sys

#: 扫描时跳过的目录（都不是我们的源码）
SKIP = re.compile(
    r"(?:^|[\\/])(?:\.venv|\.git|__pycache__|\.eval-tmp|\.pytest-tmp|\.revert-backup)(?:[\\/]|$)"
    r"|pytest-cache-files|\.egg-info(?:[\\/]|$)"
    r"|(?:^|[\\/])\.mewcode[\\/](?:sessions?|session|file-history|history|pr)(?:[\\/]|$)"
)

TEXT_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml", ".md", ".cfg", ".ini"}

#: ① 硬编码凭据。这些都是「有形状」的密钥，正则会比人眼可靠。
SECRET_PATTERNS: list[tuple[str, str]] = [
    ("API key", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("GitHub 细粒度 token", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("AWS Access Key", r"AKIA[0-9A-Z]{16}"),
    ("私钥", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

#: ② 裸 TODO/FIXME：要求后面跟 issue 号（#123）或人名（@someone）
#: 只认**注释里的**待办标记：必须带注释符前缀（# // /* ），
#: 这样 "todo",（一个数据值）不会被误判成待办 —— 实测踩到过。
#: 并且要求同行的 40 字符内出现 issue 号（#123）或负责人（@someone）。
BARE_TODO = re.compile(
    r"(?:#|//|/\*)\s*(TODO|FIXME|XXX)(?![^\n]{0,40}(?:#\d+|@[\w\-]+))"
)

#: ③ 只检查这些后缀的末尾换行（markdown 也建议有，但先只管代码）
NEWLINE_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml"}


def _git_tracked() -> list[pathlib.Path] | None:
    """git 跟踪的文件列表；不在仓库里就返回 None。

    **为什么优先用它**：这条规范针对的是「别把密钥提交上去」。
    而 .mewcode/config.yaml 里有本地 api_key 是**正常的** ——
    它被 .gitignore 挡着，永远不会进仓库。扫工作树会把这种
    正当的本地配置当成违规。实测踩到过。
    """
    import subprocess

    try:
        r = subprocess.run(
            ["git", "ls-files", "-z"], capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    names = [n for n in r.stdout.decode("utf-8", "replace").split("\0") if n]
    return [pathlib.Path(n) for n in names]


def _iter_files(roots: list[str]) -> list[pathlib.Path]:
    if not roots:
        tracked = _git_tracked()
        if tracked is not None:
            return [
                p for p in tracked
                if p.is_file() and not SKIP.search(str(p))
                and p.suffix.lower() in TEXT_SUFFIXES
            ]
    out: list[pathlib.Path] = []
    for r in roots or ["."]:
        base = pathlib.Path(r)
        if base.is_file():
            out.append(base)
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or SKIP.search(str(p)):
                continue
            if p.suffix.lower() in TEXT_SUFFIXES:
                out.append(p)
    return out


def check_secrets(path: pathlib.Path, text: str) -> list[str]:
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, pat in SECRET_PATTERNS:
            if re.search(pat, line):
                # 文档里为了说明"不要这么写"而展示的形状，允许
                if "check_standards.py" in str(path):
                    continue
                bad.append(f"  {path}:{i}  [硬编码{label}]  {line.strip()[:70]}")
    return bad


def check_bare_todo(path: pathlib.Path, text: str) -> list[str]:
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        m = BARE_TODO.search(line)
        if m:
            bad.append(
                f"  {path}:{i}  [裸 {m.group(0)}，没有关联 issue 或负责人]  "
                f"{line.strip()[:60]}"
            )
    return bad


def check_trailing_newline(path: pathlib.Path, raw: bytes) -> list[str]:
    if path.suffix.lower() not in NEWLINE_SUFFIXES:
        return []
    if raw and not raw.endswith(b"\n"):
        return [f"  {path}  [文件末尾没有换行]"]
    return []


def main(argv: list[str]) -> int:
    roots = argv[1:]
    files = _iter_files(roots)

    failures: list[str] = []
    self_path = pathlib.Path(__file__).resolve()
    for p in files:
        if p.resolve() == self_path:
            continue      # 本文件在讲这些概念，必然包含这些词
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        failures += check_secrets(p, text)
        failures += check_bare_todo(p, text)
        failures += check_trailing_newline(p, raw)

    if failures:
        print(f"❌ 内部规范检查未通过（{len(failures)} 处，扫描了 {len(files)} 个文件）\n")
        for f in failures[:80]:
            print(f)
        if len(failures) > 80:
            print(f"\n…还有 {len(failures) - 80} 处未显示")
        print(
            "\n修法：按上面每条的文件和行号改掉。\n"
            "如果某条检查本身不合理，应该去改这个脚本并说明理由，而不是绕过它。"
        )
        return 1

    print(f"✅ 内部规范检查通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

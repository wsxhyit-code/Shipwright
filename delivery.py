"""把改动送到远端：提交 → 推 `agent/*` 分支 → 开 PR。

`tools/create_pr.py`（agent 里）和 `ci/apply_and_open_pr.py`（CI 里）**共用这一份**。
不共用的话就是两套"开 PR"的实现，而它们的行为必须一致 —— 否则"agent 自己开的 PR"
和"CI 开的 PR"会出现细微差别，这种差别在出事故时最难查。

## 承重的护栏只有三条，跟"谁推送"无关

这个模块**不关心是 agent 还是 CI 在调它**。它守的是：

  ① **只推 `agent/*` 分支。** `push_branch` 会拒绝任何不以 `agent/` 开头的分支名，
     也拒绝和基线分支同名的分支。这是结构性保证：调用方**没法**通过这个函数
     推到 `main` —— 不是"我们记得别推"，而是这条路走不通。

  ② **不合并。** 这里只开 PR。PR 是**提议**，合并由人点。

  ③ **验证先于推送。** 顺序由调用方保证（见 `tools/create_pr.py` 的 execute），
     这个模块只负责"推"，不负责"判断该不该推"。

## 为什么把"AI 不能推送"从铁律里降级

原先的设计是"AI 只产出补丁、永不推送"，靠一个 hook 拦 `git push`。
实测那个 hook **能被 5 种写法绕过**（`git -C . push`、`python -c "subprocess.run(
['git','push'])"`、`git\\ push`、`$(which git) push`、`git -c k=v push`）——
而 `git -C <目录> push` 恰恰是 agent 最常用的写法。

也就是说那条"铁律"实际上是一个可绕过的正则。与其用它假装拦住，不如：
**明确允许推送，然后把真正承重的三条守住。**

（另外：容器模式下 agent 连不上远端 —— 那才是物理保证。见 `docker/run-agent.sh`。）
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

#: 允许推送的分支前缀。`push_branch` 靠这个结构性保证"推不到基线分支"。
BRANCH_PREFIX = "agent/"

#: 即使前缀对了，也不允许推这些名字（万一有人把前缀配成别的）
PROTECTED_BRANCHES = frozenset(
    {"main", "master", "develop", "development", "trunk", "release", "prod", "production"}
)


class DeliveryError(Exception):
    """推送 / 开 PR 过程中的可读失败。调用方据此给出人能接手的提示。"""


# ---------------------------------------------------------------------------
# 子进程与 git
# ---------------------------------------------------------------------------


def run(
    argv: list[str], cwd: str | Path, timeout: int = 900, check: bool = False
) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 合并后的输出)。

    刻意不用 `check=True` 抛异常：调用方需要拿到退出码和输出，
    好把失败原因原样透给模型或写进 FAILURE.md。
    """
    try:
        p = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）：{' '.join(argv)}"
    except FileNotFoundError as e:
        return 127, f"命令不存在：{e}"
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise DeliveryError(f"{' '.join(argv)} 失败（退出码 {p.returncode}）:\n{out}")
    return p.returncode, out


def git(repo: str | Path, *args: str, check: bool = True) -> tuple[int, str]:
    return run(["git", *args], repo, check=check)


def split_command(cmd: str) -> list[str]:
    """拆一条验证命令。

    与 `tools/create_pr.py` 同样的引号处理：Windows 上 `shlex.split(posix=False)`
    会把引号**保留在 token 里**，于是 `"C:\\Program Files\\python.exe"` 会变成
    带引号的路径，执行时退出码 127。这里手工把成对的引号剥掉。
    """
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        return [cmd]
    return [
        t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
        for t in tokens
    ] or [cmd]


# ---------------------------------------------------------------------------
# 分支名
# ---------------------------------------------------------------------------


def slugify(text: str, maxlen: int = 40) -> str:
    """把 PR 标题变成安全的分支名片段。

    只保留 [a-z0-9-]，中文标题会被压成空串 —— 这时退回 `change`，
    避免出现 `agent/--1234` 这种怪分支名。
    """
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen] or "change"


def derive_branch(title: str, sha: str, prefix: str = BRANCH_PREFIX) -> str:
    """分支名 = `<前缀><标题片段>-<短 sha>`。

    带 sha 是为了**重试幂等**：同一个改动重跑会得到同一个分支名，
    跑第二遍时用 `-B` 复用分支而不是报"分支已存在"。
    """
    return f"{prefix}{slugify(title)}-{sha}"


def current_branch(repo: str | Path) -> str:
    code, out = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return out.strip() if code == 0 else ""


def head_sha(repo: str | Path, short: bool = True) -> str:
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    code, out = git(repo, *args, check=False)
    return out.strip() if code == 0 else ""


def guard_branch(branch: str, base: str, remote: str = "origin") -> None:
    """推之前的结构性检查。**这是"绝不推基线分支"的落点。**

    不通过就抛 `DeliveryError` —— 不做"警告后继续"，
    因为推错分支是这类流程里最难挽回的事故。
    """
    if branch != branch.strip():
        raise DeliveryError(f"分支名首尾有空白：{branch!r}")
    if not branch:
        raise DeliveryError("分支名为空，拒绝推送")
    if branch in PROTECTED_BRANCHES or branch.split("/")[-1] in PROTECTED_BRANCHES:
        raise DeliveryError(
            f"拒绝推送受保护的分支名 {branch!r}（{sorted(PROTECTED_BRANCHES)}）"
        )
    if branch in (base, f"{remote}/{base}"):
        raise DeliveryError(
            f"拒绝推送基线分支 {branch!r}（base={base!r}）——"
            "改动只能落在派生分支上"
        )
    if not branch.startswith(BRANCH_PREFIX):
        raise DeliveryError(
            f"拒绝推送 {branch!r}：分支名必须以 {BRANCH_PREFIX!r} 开头。"
            "这条限制是结构性的 —— 它保证这个函数**没法**推到基线分支。"
        )


# ---------------------------------------------------------------------------
# 提交与推送
# ---------------------------------------------------------------------------


def commit_all(repo: str | Path, message: str) -> tuple[bool, str]:
    """把工作区全部暂存并提交。返回 (是否有改动, 说明)。

    没有改动时返回 False 而**不是**提交一个空 commit ——
    空 commit 会在远端留下一个无意义的条目。
    """
    code, _ = git(repo, "add", "-A", check=False)
    if code != 0:
        return False, "git add 失败"
    code, _ = git(repo, "diff", "--cached", "--quiet", check=False)
    if code == 0:
        return False, "没有需要提交的改动"
    code, out = git(repo, "commit", "-m", message, check=False)
    if code != 0:
        return False, f"git commit 失败：\n{out}"
    return True, ""


def checkout_branch(repo: str | Path, branch: str) -> None:
    """切到（或重置到）该分支。

    用 `-B` 而不是 `checkout -b`：重试时分支已存在不会失败。
    """
    guard_branch(branch, base="", remote="")
    code, out = git(repo, "checkout", "-B", branch, check=False)
    if code != 0:
        raise DeliveryError(f"切换分支 {branch!r} 失败：\n{out}")


def push_branch(
    repo: str | Path,
    branch: str,
    base: str,
    remote: str = "origin",
    timeout: int = 900,
) -> tuple[bool, str]:
    """把分支推到远端。返回 (是否成功, 说明)。

    推之前一定过 `guard_branch` —— 所有推送路径都必须走这里，
    这样"绝不推基线分支"就只有一处需要审。
    """
    try:
        guard_branch(branch, base, remote)
    except DeliveryError as e:
        return False, str(e)

    code, out = git(repo, "push", "-u", remote, branch, check=False)
    if code != 0:
        return False, f"git push 失败：\n{out}"
    return True, f"已推送到 {remote}/{branch}"


# ---------------------------------------------------------------------------
# 开 PR
# ---------------------------------------------------------------------------

_GH_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$")


def default_api_base() -> str:
    return (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def open_pr(
    repo: str | Path,
    title: str,
    body_file: Path,
    base: str,
    branch: str,
    api_base: str = "",
    remote: str = "origin",
) -> tuple[bool, str]:
    """开 PR：优先 `gh`，其次 REST API，都不行就给一条人能接手的提示。

    `api_base` 可以指向 GitHub Enterprise / Gitea 等自建实例
    （`https://ghe.corp/api/v3`），也可用环境变量 `GITHUB_API_URL`。
    这也是让这条路径**可测**的关键：测试可以把它指到本地服务，
    从而真实验证请求 URL、请求头和 payload。
    """
    api_base = (api_base or default_api_base()).rstrip("/")

    if shutil.which("gh"):
        code, out = run(
            ["gh", "pr", "create", "--title", title, "--body-file", str(body_file),
             "--base", base, "--head", branch],
            repo,
        )
        return (code == 0, out.strip() or "PR 已创建")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return False, "既没有 gh 命令，也没有 GITHUB_TOKEN —— 无法自动开 PR"

    code, url = git(repo, "remote", "get-url", remote, check=False)
    m = _GH_URL_RE.search(url.strip())
    if not m:
        return False, (
            f"无法从 {remote} 的 URL 里解析出 owner/repo"
            f"（只支持 github.com 的地址）：{url.strip()[:120]}"
        )

    owner, name = m.group(1), m.group(2)
    import urllib.error
    import urllib.request

    body_text = body_file.read_text(encoding="utf-8") if body_file.exists() else ""
    payload = json.dumps(
        {"title": title, "head": branch, "base": base, "body": body_text}
    ).encode()
    req = urllib.request.Request(
        f"{api_base}/repos/{owner}/{name}/pulls",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return True, f"PR 已创建：{data.get('html_url')}"
    except urllib.error.HTTPError as e:
        return False, f"GitHub API {e.code}: {e.read().decode()[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, f"调用 GitHub API 失败: {e}"

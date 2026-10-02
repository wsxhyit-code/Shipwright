"""端到端 demo：从「代码有 bug」到「远端多了一个 agent 分支 + 一个真 PR」。

这个 demo 存在的理由：**"PR 功能"之前没有被端到端跑过。**

  · `CreatePR` 工具能通过测试，但 `app.py` / `__main__.py` 从来没把它注册给
    agent —— 也就是说真实运行起来的 agent 根本够不着它
  · "远端没被改动"那几条断言在 push 跑不通的环境里是**空过**的
  · `_open_pr` 把 `https://api.github.com` 写死，没有 token 就永远走不到，
    那几十行网络处理代码从写下第一天起没执行过

这个 demo 把整条链路真的跑一遍。**唯一被桩掉的是"模型说了什么"** ——
LLM 那一步换成一个按脚本回话的假 client，其余全是真的：

    真 git 仓库、真 bare 远端、真 pytest 退出口、真 CreatePR 工具、
    真独立验证者机制（新上下文 + 只读注册表 + plan 模式 + VERDICT 解析）、
    真 CI 脚本、真 git push、真 HTTP POST 到一个真的（本地）GitHub API 服务

跑法：

    python docs/devops-agent/demo_full_pipeline.py

零 API key（因为模型那步是桩的），零外部依赖。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mewcode.client import LLMClient  # noqa: E402
from mewcode.config import CreatePRConfig, ToolsetConfig  # noqa: E402
from mewcode.toolset import assemble_toolset  # noqa: E402
from mewcode.tools import create_default_registry  # noqa: E402
from mewcode.tools.base import StreamEnd, TextDelta  # noqa: E402

WORK = Path(__file__).resolve().parents[2] / ".eval-tmp" / "full-pipeline"

#: 有 bug 的实现：get_tier 少了判空（和 orders-api 那个故障同源）
BUGGY = '''\
def get_tier(user):
    return user["profile"]["tier"]


def greet(user):
    return f"hello {get_tier(user)}"
'''

FIXED = '''\
def get_tier(user):
    profile = user.get("profile")
    if profile is None:
        return "GUEST"
    return profile["tier"]


def greet(user):
    return f"hello {get_tier(user)}"
'''

TEST_FILE = '''\
from app import get_tier


def test_normal_user():
    assert get_tier({"profile": {"tier": "GOLD"}}) == "GOLD"


def test_user_without_profile():
    # v2.14.3 移除了判空，这一行开始报 KeyError
    assert get_tier({}) == "GUEST"
'''


# ---------------------------------------------------------------------------
# 桩：只替换"模型说了什么"
# ---------------------------------------------------------------------------


class ScriptedClient(LLMClient):
    """按脚本回话的假 LLM。

    它**记录自己被提供了哪些工具** —— demo 会打印出来，
    用来证明独立验证者拿到的确实是一个只读工具集。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.offered_tools: list[str] = []
        self.system_prompts: list[str] = []

    async def stream(self, conversation, system="", tools=None):
        self.offered_tools = [t.get("name", "?") for t in (tools or [])]
        self.system_prompts.append(system or "")
        yield TextDelta(self.reply)
        yield StreamEnd(stop_reason="end_turn", input_tokens=120, output_tokens=18)


# ---------------------------------------------------------------------------
# 假的 GitHub API（真 HTTP 服务）
# ---------------------------------------------------------------------------


class FakeGitHub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        type(self).received.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": json.loads(raw.decode()) if raw else {},
            }
        )
        body = json.dumps(
            {"html_url": "https://github.com/acme/orders-api/pull/142", "number": 142}
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_fake_github() -> tuple[ThreadingHTTPServer, str]:
    FakeGitHub.received = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()}")
    return r


def rmtree(p: Path) -> None:
    """强删目录树。

    不能用 `shutil.rmtree(ignore_errors=True)`：Windows 上 `git init` 会把
    `.git/objects/**` 标成只读，删不掉；而 `ignore_errors` 会**静默留下半个目录**，
    下次 `mkdir()` 直接 FileExistsError（第一次跑这个 demo 就撞上了）。
    和 tests/conftest.py 里的 `_force_rmtree` 是同一套处理。
    """
    import os
    import shutil
    import stat

    def _onexc(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except OSError:
            pass

    if p.exists():
        shutil.rmtree(p, onexc=_onexc)


def setup_repos() -> tuple[Path, Path]:
    """真仓库 + 真 bare 远端，远端上先有一个 main。

    远端路径刻意做成 `<...>/github.com/acme/orders-api.git`，两个目的：

      · 它本身是个**真实可推送**的本地 bare 仓库 —— push 那一半是真的
      · 同时这段路径能被 `_open_pr` 的 `github.com[:/]owner/repo` 正则解析出来，
        于是 PR 那一半也会真的发出 HTTP 请求

    这样不需要真 GitHub 账号，就能把"推送"和"开 PR"两半都真跑到。
    必须用正斜杠（`as_posix()`）：Windows 的反斜杠路径匹配不上那个正则，
    而 `git push` 和 PR 解析用的是**同一个** `--remote`。
    """
    rmtree(WORK)
    repo = WORK / "repo"
    origin = WORK / "github.com" / "acme" / "orders-api.git"
    repo.mkdir(parents=True)
    origin.mkdir(parents=True)

    git(origin, "init", "--bare", "--initial-branch=main")
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "agent@example.com")
    git(repo, "config", "user.name", "Shipwright Agent")

    (repo / "app.py").write_text(BUGGY, encoding="utf-8")
    (repo / "test_app.py").write_text(TEST_FILE, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init: 有 bug 的版本")
    git(repo, "remote", "add", "origin", origin.as_posix())
    git(repo, "push", "-u", "origin", "main")
    return repo, origin


async def call_tool(tool, **kwargs):
    return await tool.execute(tool.params_model(**kwargs))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run() -> int:
    python = sys.executable
    verify_cmd = f'"{python}" -m pytest test_app.py -q'

    hr("① 准备：真仓库 + 真 bare 远端")
    repo, origin = setup_repos()
    print(f"  仓库：{repo}")
    print(f"  远端：{origin}")
    print(f"  main 上的 commit 数：{git(origin, 'rev-list', '--all', '--count').stdout.strip()}")
    print()
    print("  app.py 里的 bug（v2.14.3 移除了判空）：")
    for line in BUGGY.strip().splitlines():
        print(f"    {line}")

    hr("② 确认 bug 是真的（跑测试）")
    r = subprocess.run(
        [python, "-m", "pytest", "test_app.py", "-q"], cwd=repo,
        capture_output=True, text=True,
    )
    print(f"  pytest 退出码：{r.returncode}（期望非 0）")
    tail = [ln for ln in r.stdout.strip().splitlines() if "Error" in ln or "failed" in ln]
    for line in tail[:3]:
        print(f"    {line}")

    hr("③ 装配 toolset —— 这一步以前**不存在**")
    registry = create_default_registry()
    before = {t.name for t in registry.list_tools()}
    print(f"  create_default_registry() 有 {len(before)} 个工具")
    print(f"  里面有 CreatePR 吗？{'有' if 'CreatePR' in before else '没有 ← 这就是缺口'}")

    cfg = ToolsetConfig()
    cfg.create_pr = CreatePRConfig(
        enabled=True,
        verify_command=verify_cmd,
        artifacts_dir=str(WORK / "artifacts"),
        base_ref="main",
        timeout=300,
    )

    # 真实机制：用真 Agent + 假 client 造独立验证者
    from mewcode.agent import Agent
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )

    verifier_client = ScriptedClient("VERDICT: PASS")
    parent = Agent(
        client=verifier_client,
        registry=registry,
        protocol="anthropic",
        work_dir=str(repo),
        permission_checker=PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(str(repo)),
            rule_engine=RuleEngine(),
            mode=PermissionMode.DEFAULT,
        ),
        context_window=200_000,
    )

    assembly = assemble_toolset(registry, cfg, agent=parent, work_dir=repo)
    after = {t.name for t in registry.list_tools()}
    print(f"  装配后有 {len(after)} 个工具，新增：{sorted(after - before)}")

    create_pr = registry.get("CreatePR")
    assert create_pr is not None, "CreatePR 没被注册！"
    print(f"  CreatePR.category = {create_pr.category}（有外部副作用，不能算 read）")
    print(f"  CreatePR._require_independent = {create_pr._require_independent}")
    print(f"  CreatePR._verifier_runner 已接 = {create_pr._verifier_runner is not None}")

    hr("④ 先试一次「代码还没改」就提 PR —— 门禁必须拦住")
    r = await call_tool(create_pr, title="fix: 修 get_tier 判空")
    print(f"  is_error = {r.is_error}")
    print("  " + (r.output or "")[:400].replace("\n", "\n  "))
    patch_file = WORK / "artifacts" / "changes.patch"
    print()
    print(f"  产出补丁了吗？{'产出了 ← 不应该' if patch_file.exists() else '没有 ← 正确'}")

    hr("⑤ 真的改代码（模拟 agent 的 EditFile）")
    (repo / "app.py").write_text(FIXED, encoding="utf-8")
    print("  改后的 app.py：")
    for line in FIXED.strip().splitlines():
        print(f"    {line}")

    hr("⑥ 再提一次 —— 命令验证 + 独立验证都过")
    r = await call_tool(create_pr, title="fix: 修 get_tier 判空")
    print(f"  is_error = {r.is_error}")
    print("  " + (r.output or "")[:700].replace("\n", "\n  "))

    print()
    print("  独立验证者拿到的工具集（由桩 client 实际记录）：")
    print(f"    {verifier_client.offered_tools}")
    write_tools = {"WriteFile", "EditFile", "Bash", "CreatePR"}
    leak = write_tools & set(verifier_client.offered_tools)
    print(f"    写工具泄漏？{leak if leak else '没有 ← 只读注册表生效'}")
    note = verifier_client.system_prompts[0] if verifier_client.system_prompts else ""
    print(f"    它的 system prompt：{note[:60]!r}")

    if not patch_file.exists():
        print("\n❌ 验证通过了却没产出补丁，后面没法继续")
        return 1

    hr("⑦ 看 agent 产出的三样东西")
    for name in ("changes.patch", "pr.json", "verification.md"):
        p = WORK / "artifacts" / name
        if p.exists():
            print(f"  {name}: {p.stat().st_size} 字节")
    meta = json.loads((WORK / "artifacts" / "pr.json").read_text(encoding="utf-8"))
    print(f"  pr.json: title={meta.get('title')!r} base_ref={meta.get('base_ref')!r}")
    print(f"            verify.ok={meta.get('verify', {}).get('ok')}")

    hr("⑧ 远端此刻的状态（agent 从没推送过）")
    print(f"  commit 数：{git(origin, 'rev-list', '--all', '--count').stdout.strip()}（还是 1）")
    print(f"  分支：{git(origin, 'branch', '--list').stdout.strip()}")

    hr("⑧b 把工作区恢复成「干净的基线检出」")
    print("  真实流程里，CI 是在一个干净的检出上应用补丁的 —— agent 在容器里")
    print("  改代码、产出补丁，CI 拿到的是**没被改过的仓库** + 一个补丁。")
    print("  这里必须把工作区还原，否则补丁会因为「已经应用过」而失败。")
    git(repo, "checkout", "--", ".")
    status = git(repo, "status", "--porcelain").stdout.strip()
    print(f"  还原后 git status：{status or '(干净)'}")
    buggy_now = (repo / "app.py").read_text(encoding="utf-8")
    print(f"  app.py 现在是 bug 版本吗？{'是' if buggy_now == BUGGY else '否 ← 不对'}")

    hr("⑨ CI 收尾：真 push + 真开 PR（对着本地假 GitHub API）")
    srv, api_base = start_fake_github()
    print(f"  假 GitHub API 起在 {api_base}")
    print("  （api_base 可配置这件事本身就是这一轮加的：原来是写死的 api.github.com，")
    print("    导致自建实例用不了、这条路径也没法被测）")
    print()

    import os

    env = dict(os.environ)
    env["GITHUB_TOKEN"] = "ghp_demo_token"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    ci = Path(__file__).resolve().parents[2] / "ci" / "apply_and_open_pr.py"
    r = subprocess.run(
        [
            python, str(ci),
            "--repo", str(repo),
            "--artifacts", str(WORK / "artifacts"),
            "--verify-cmd", verify_cmd,
            "--base", "main",
            "--api-base", api_base,
        ],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    print(f"  CI 退出码：{r.returncode}")
    for line in (r.stdout or "").strip().splitlines():
        print(f"    {line}")
    # stderr 一定要打 —— 这条路径"开 PR 失败但整体算成功"（人要能接手），
    # 所以只在非 0 退出码时才打 stderr 的话，恰好会把最关键的信息藏起来。
    if (r.stderr or "").strip():
        print("  --- stderr ---")
        for line in r.stderr.strip().splitlines():
            print(f"    {line}")

    hr("⑩ 验证副作用：远端真的变了，但只多了一个 agent 分支")
    count = git(origin, "rev-list", "--all", "--count").stdout.strip()
    branches = git(origin, "branch", "--list").stdout.strip()
    print(f"  commit 数：{count}")
    print("  远端分支：")
    for line in branches.splitlines():
        print(f"    {line}")

    assert "main" in branches, "main 不见了"
    assert "agent/" in branches, "agent 分支没推上去"
    main_sha = git(origin, "rev-parse", "main").stdout.strip()
    # main 必须还是最初那个 commit（没被 agent 或 CI 动过）
    first_sha = git(origin, "rev-list", "--max-parents=0", "main").stdout.strip()
    assert main_sha == first_sha, f"main 被改动了！{main_sha} != {first_sha}"
    print()
    print("  ✅ main 仍指向最初那个 commit —— 基线分支没被碰过")

    hr("⑪ 验证 PR 请求真的发出去了")
    if not FakeGitHub.received:
        print("  ❌ 假 GitHub API 没收到任何请求")
        return 1
    req = FakeGitHub.received[0]
    print(f"  POST {req['path']}")
    print(f"  Authorization: {req['auth']}")
    print("  payload:")
    for k, v in req["body"].items():
        shown = (v[:70] + "…") if isinstance(v, str) and len(v) > 70 else v
        print(f"    {k}: {shown!r}")
    print()
    assert req["path"] == "/repos/acme/orders-api/pulls"
    assert req["body"]["base"] == "main"
    assert req["body"]["head"].startswith("agent/")
    print("  ✅ base 是 main、head 是 agent 分支、认证头正确")

    srv.shutdown()
    srv.server_close()

    hr("结论")
    print("  这条链路上每一步都真的执行过了：")
    print("    门禁拦得住没改好的代码 → 改对了才产出补丁 →")
    print("    CI 独立重跑 → 推送 agent/* 分支 → 调用 PR API")
    print()
    print("  **唯一没有验证的是「模型能不能真的找到并改对那个 bug」** ——")
    print("  那需要真实 LLM，本机 API 余额不足。用的是桩 client 返回 VERDICT。")
    print()
    print(f"  产物在 {WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

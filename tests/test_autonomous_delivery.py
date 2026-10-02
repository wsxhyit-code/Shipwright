"""自治交付的测试：agent 自己 提交 → 推 `agent/*` → 开 PR。

## 为什么允许 agent 自己推送

原先的设计是"AI 只产出补丁、永不推送"，靠一个 hook 拦 `git push`。
实测那个 hook **能被 5 种写法绕过**（`git -C . push`、
`python -c "subprocess.run(['git','push'])"`、`git\\ push`、
`$(which git) push`、`git -c k=v push`）—— 而 `git -C <目录> push`
恰恰是 agent 最常用的写法。也就是说那条"铁律"实际是个可绕过的正则。

与其用它假装拦住，不如明确允许推送，再把**真正承重的三条**守住：

  ① 绝不推基线分支（结构性：`guard_branch` 只放行 `agent/` 前缀）
  ② 绝不合并（只开 PR，PR 是提议）
  ③ 验证先于推送（顺序在 `execute` 里硬编码 —— 本文件里有一条专门钉它）

另外：容器模式下 agent 连不上远端（无网络出口 + 无凭据 + 无宿主 home），
那才是物理保证；本地 TUI 模式没有这层物理隔离，所以护栏必须在代码里。

    pytest tests/test_autonomous_delivery.py -v
"""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mewcode import delivery
from mewcode.tools.create_pr import CreatePRTool, VerifyResult

# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check:
        assert r.returncode == 0, f"git {' '.join(args)} 失败: {r.stderr}"
    return r.stdout


def ok_result() -> VerifyResult:
    return VerifyResult(ok=True, command="pytest -q", exit_code=0,
                        output="3 passed", elapsed=0.1)


def bad_result() -> VerifyResult:
    return VerifyResult(ok=False, command="pytest -q", exit_code=1,
                        output="1 failed", elapsed=0.2)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    git(d, "init", "-b", "main")
    git(d, "config", "user.email", "t@e.com")
    git(d, "config", "user.name", "T")
    (d / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-m", "init")
    return d


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """bare 远端。路径刻意含 `github.com/acme/demo.git`：

    `open_pr` 靠 `github.com[:/]owner/repo` 正则从远端 URL 里取 owner/repo，
    而 agent 的推送和开 PR 用的是**同一个** remote —— 所以远端路径必须
    既真实可推送、又能被那个正则解析出来。这样不需要真 GitHub 账号
    就能把两段都跑到。
    """
    d = tmp_path / "github.com" / "acme" / "demo.git"
    d.mkdir(parents=True)
    git(d, "init", "--bare", "-b", "main")
    return d


def wire_origin(repo: Path, origin: Path) -> None:
    git(repo, "remote", "add", "origin", origin.as_posix())
    git(repo, "push", "-u", "origin", "main")


def make_tool(repo: Path, origin: Path, *, mode: str, verify=ok_result,
              **kw) -> CreatePRTool:
    """造一个工具，验证器用桩（真实验证者需要 LLM）。

    `verify` 传 `bad_result` 就模拟"命令验证没过"。
    """
    opts = {
        "work_dir": str(repo),
        "verify_command": "pytest -q",
        "artifacts_dir": str(repo / ".mewcode" / "pr"),
        "base_ref": "main",
        "verifier": lambda: verify(),
        "mode": mode,
        "remote": "origin",
    }
    opts.update(kw)
    return CreatePRTool(**opts)


async def call(tool: CreatePRTool, **kwargs):
    return await tool.execute(tool.params_model(**kwargs))


async def independent_pass(_desc: str) -> str:
    return "看过了。\nVERDICT: PASS"


# ---------------------------------------------------------------------------
# 假的 GitHub API
# ---------------------------------------------------------------------------


class _FakeGitHub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        type(self).received.append(
            {"path": self.path, "body": json.loads(raw) if raw else {}}
        )
        body = json.dumps({"html_url": "https://github.com/acme/demo/pull/1"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_github(monkeypatch):
    _FakeGitHub.received = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def remote_commits(origin: Path) -> str:
    return git(origin, "rev-list", "--all", "--count").strip()


def remote_branches(origin: Path) -> list[str]:
    out = git(origin, "branch", "--list")
    return [b.strip().lstrip("* ") for b in out.splitlines() if b.strip()]


# ---------------------------------------------------------------------------
# 一、结构性护栏：推不到基线分支
# ---------------------------------------------------------------------------


class TestBranchGuard:
    """`guard_branch` 是"绝不推基线分支"的唯一落点。

    之所以要有它，是因为"我们记得别推 main"不是保证 ——
    而**这个函数只放行 `agent/` 前缀**，所以调用方没法推别的。
    """

    @pytest.mark.parametrize("branch", ["main", "master", "develop", "trunk",
                                        "release", "production"])
    def test_protected_names_rejected(self, branch):
        with pytest.raises(delivery.DeliveryError):
            delivery.guard_branch(branch, base="main", remote="origin")

    def test_base_branch_rejected_even_with_prefix(self):
        """基线分支一定被拒。

        注意**不去钉是哪一层守卫拦下的** —— `main` 同时命中
        "受保护分支名"和"等于基线"两条，哪个先报都算对。
        钉死具体消息会让测试在无关的守卫顺序调整时误报。
        """
        with pytest.raises(delivery.DeliveryError) as exc:
            delivery.guard_branch("main", base="main", remote="origin")
        assert "拒绝" in str(exc.value)

    def test_base_branch_check_fires_when_name_is_not_protected(self):
        """用一个不在受保护名单里的基线名，单独验证"等于基线"这一条守卫。"""
        with pytest.raises(delivery.DeliveryError, match="基线分支"):
            delivery.guard_branch("agent/base-copy", base="agent/base-copy",
                                  remote="origin")

    def test_remote_qualified_base_rejected(self):
        with pytest.raises(delivery.DeliveryError):
            delivery.guard_branch("origin/main", base="main", remote="origin")

    def test_branch_without_agent_prefix_rejected(self):
        """核心：这是结构性的，不是"记得别推"。

        `feature/x` 名字看起来无害，但去掉前缀要求就失去了
        "这个函数推不到基线"这个性质。
        """
        with pytest.raises(delivery.DeliveryError, match="agent/"):
            delivery.guard_branch("feature/x", base="main", remote="origin")

    def test_empty_and_whitespace_rejected(self):
        for bad in ("", "   ", " agent/x", "agent/x "):
            with pytest.raises(delivery.DeliveryError):
                delivery.guard_branch(bad, base="main", remote="origin")

    def test_normal_agent_branch_passes(self):
        delivery.guard_branch("agent/fix-abc123", base="main", remote="origin")

    def test_derived_branch_always_passes_the_guard(self):
        """任何标题派生出来的分支都必须过守卫 —— 否则正常流程会自己卡住。"""
        for title in ("main", "master", "修复导出", "", "  ", "agent/main", "a/b/c"):
            branch = delivery.derive_branch(title, "abc1234")
            delivery.guard_branch(branch, base="main", remote="origin")

    def test_push_branch_returns_false_instead_of_raising(self, repo, origin):
        """`push_branch` 把守卫失败转成 (False, 原因)，好让工具层给出可读结果。

        这条**不需要真能推送** —— 守卫在发任何 git 命令之前就返回了。
        所以只 `remote add`，不 push。
        """
        git(repo, "remote", "add", "origin", origin.as_posix())
        ok, msg = delivery.push_branch(repo, "main", base="main", remote="origin")
        assert ok is False
        assert "拒绝" in msg
        # 确认它真的没尝试推送（远端分支列表还不存在任何东西）
        assert git(origin, "branch", "--list").strip() == ""


# ---------------------------------------------------------------------------
# 二、模式语义
# ---------------------------------------------------------------------------


class TestModes:
    def test_bad_mode_rejected_at_construction(self, repo):
        with pytest.raises(ValueError, match="mode"):
            make_tool(repo, repo, mode="yolo")

    async def test_patch_mode_does_not_push(self, repo, origin, requires_real_push):
        """默认模式行为和加这个开关之前完全一致：只出补丁。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="patch")
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, r.output
        assert "未推送" in r.output
        assert remote_commits(origin) == before
        assert remote_branches(origin) == ["main"]
        assert (repo / ".mewcode" / "pr" / "changes.patch").exists()

    async def test_push_mode_pushes_agent_branch(self, repo, origin,
                                                 requires_real_push):
        wire_origin(repo, origin)
        before = remote_commits(origin)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push")
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, r.output
        branches = remote_branches(origin)
        assert "main" in branches
        assert any(b.startswith("agent/") for b in branches), branches
        assert int(remote_commits(origin)) > int(before)

    async def test_push_mode_does_not_open_pr(self, repo, origin, fake_github,
                                              requires_real_push):
        wire_origin(repo, origin)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push")
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, r.output
        assert "没有**开 PR" in r.output or "没有开 PR" in r.output
        assert _FakeGitHub.received == []

    async def test_pr_mode_opens_a_pr(self, repo, origin, fake_github,
                                      requires_real_push):
        wire_origin(repo, origin)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr")
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, r.output
        assert "PR 已创建" in r.output
        assert len(_FakeGitHub.received) == 1

        payload = _FakeGitHub.received[0]["body"]
        assert payload["base"] == "main"
        assert payload["head"].startswith("agent/")
        # ★ PR 的 base 是基线、head 是 agent 分支 —— 不能自己合自己
        assert payload["head"] != payload["base"]

    async def test_pr_mode_leaves_main_untouched(self, repo, origin, fake_github,
                                                 requires_real_push):
        wire_origin(repo, origin)
        main_sha_before = git(origin, "rev-parse", "main").strip()

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr")
        await call(tool, title="fix: 改 VALUE")

        assert git(origin, "rev-parse", "main").strip() == main_sha_before, (
            "基线分支被改动了！"
        )

    async def test_pr_mode_never_merges(self, repo, origin, fake_github,
                                        requires_real_push):
        """只开 PR，不合并。远端不应该出现任何 merge commit。"""
        wire_origin(repo, origin)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr")
        await call(tool, title="fix: 改 VALUE")

        # 远端分支数 = main + 1 个 agent 分支，而且 main 上没有新 commit
        branches = remote_branches(origin)
        assert "main" in branches
        assert len(branches) == 2, branches
        assert len(git(origin, "rev-list", "main").split()) == 1  # 只有 init
        # main 指向的那个 commit 必须还是最初的（没被 merge 或 amend）
        assert git(origin, "rev-list", "--max-parents=0", "main").strip() == (
            git(origin, "rev-parse", "main").strip()
        )


# ---------------------------------------------------------------------------
# 三、顺序不变量：验证必须在推送之前 ★
# ---------------------------------------------------------------------------


class TestGateComesBeforePush:
    """这是替代"AI 不能推送"的那条真正的铁律。

    "agent 可以自己推"之所以安全，前提是**没通过验证就绝不会推**。
    所以这里的每一条都要证明：验证没过时，远端一个 commit 都没多。
    """

    async def test_command_verify_failure_pushes_nothing(self, repo, origin,
                                                         requires_real_push):
        wire_origin(repo, origin)
        before = remote_commits(origin)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push", verify=bad_result)
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert "验证未通过" in r.output
        assert remote_commits(origin) == before, "验证没过却推了东西！"
        assert remote_branches(origin) == ["main"]

    async def test_independent_verify_failure_pushes_nothing(self, repo, origin,
                                                             requires_real_push):
        """命令验证过了，但独立验证者给了 FAIL —— 同样一个 commit 都不推。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        async def independent_fail(_desc: str) -> str:
            return "这个改动只覆盖了 happy path。\nVERDICT: FAIL"

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push",
                         verifier_runner=independent_fail,
                         require_independent=True)
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert "独立验证" in r.output
        assert remote_commits(origin) == before, "独立验证 FAIL 却推了东西！"
        assert remote_branches(origin) == ["main"]

    async def test_no_change_pushes_nothing(self, repo, origin, requires_real_push):
        """没有任何改动时不能推空 commit。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        tool = make_tool(repo, origin, mode="push")
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert "没有任何改动" in r.output
        assert remote_commits(origin) == before
        assert remote_branches(origin) == ["main"]

    async def test_pr_mode_verification_failure_also_pushes_nothing(
        self, repo, origin, fake_github, requires_real_push
    ):
        """pr 模式下同理 —— 而且 PR 也不该开出来。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr", verify=bad_result)
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert remote_commits(origin) == before
        assert _FakeGitHub.received == [], "验证没过却开了 PR！"

    async def test_independent_runs_before_anything_is_committed(self, repo, origin,
                                                                 requires_real_push):
        """独立验证者被调用时，**本地也还没有**任何提交。

        这一条比"远端没变"更强：它证明顺序是
        验证 → 提交 → 推送，而不是 提交 → 推送 → 验证。
        """
        wire_origin(repo, origin)
        seen: dict[str, str] = {}

        async def peek(_desc: str) -> str:
            seen["branch"] = delivery.current_branch(repo)
            seen["commits"] = git(repo, "rev-list", "--count", "HEAD").strip()
            return "VERDICT: PASS"

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push",
                         verifier_runner=peek, require_independent=True)
        await call(tool, title="fix: 改 VALUE")

        assert seen["branch"] == "main", "验证者被调用时已经切到 agent 分支了"
        assert seen["commits"] == "1", "验证者被调用时已经有新提交了"


# ---------------------------------------------------------------------------
# 四、失败路径要给可读结果
# ---------------------------------------------------------------------------


class TestFailureReporting:
    async def test_push_without_remote_gives_readable_error(self, repo):
        """没有配远端时，agent 应该拿到一句能自己判断原因的话，而不是异常。"""
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, repo, mode="push")
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert "交付失败" in r.output
        assert "补丁文件仍可用" in r.output
        # 关键：要说清基线分支没被动过
        assert "基线分支" in r.output

    async def test_pr_failure_still_reports_branch_pushed(self, repo, origin,
                                                          monkeypatch,
                                                          requires_real_push):
        """分支推上去了但 PR 没开成 —— 这不是彻底失败，人要能接手。"""
        wire_origin(repo, origin)
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:1")  # 必然连不上

        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr")
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, "分支推成功了，不该整体算失败"
        assert "分支已推送" in r.output
        assert "请手动开 PR" in r.output
        # 分支确实上去了
        assert any(b.startswith("agent/") for b in remote_branches(origin))

    async def test_delivery_result_is_recorded(self, repo, origin, fake_github,
                                               requires_real_push):
        """交付结果要留在工具上，供上层（与测试）读取。"""
        wire_origin(repo, origin)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr")
        await call(tool, title="fix: 改 VALUE")

        assert tool.last_delivery.get("branch", "").startswith("agent/")
        assert "PR 已创建" in tool.last_delivery.get("pr", "")


# ---------------------------------------------------------------------------
# 五、和独立验证者的配合
# ---------------------------------------------------------------------------


class TestWithIndependentVerifier:
    async def test_require_independent_without_verifier_refuses(self, repo, origin,
                                                                requires_real_push):
        """require_independent=True 却没接验证者 → 直接拒绝，绝不推送。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push", require_independent=True)
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert "独立验证" in r.output
        assert remote_commits(origin) == before

    async def test_verifier_without_verdict_is_fail_closed(self, repo, origin,
                                                           requires_real_push):
        """验证者没给 VERDICT → 按不通过处理，一个 commit 都不推。"""
        wire_origin(repo, origin)
        before = remote_commits(origin)

        async def no_verdict(_desc: str) -> str:
            return "看起来还行吧。"  # 没有 VERDICT 字样

        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="push",
                         verifier_runner=no_verdict, require_independent=True)
        r = await call(tool, title="fix: 改 VALUE")

        assert r.is_error
        assert remote_commits(origin) == before, "验证者没给结论却推了东西！"

    async def test_happy_path_with_real_verifier_machinery(self, repo, origin,
                                                           fake_github,
                                                           requires_real_push):
        """走一遍完整链路：命令验证 + 独立验证 + 推 + 开 PR。"""
        wire_origin(repo, origin)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        tool = make_tool(repo, origin, mode="pr",
                         verifier_runner=independent_pass,
                         require_independent=True)
        r = await call(tool, title="fix: 改 VALUE")

        assert not r.is_error, r.output
        assert "命令验证" in r.output
        assert "独立验证（VERDICT: PASS）" in r.output
        assert "PR 已创建" in r.output
        assert _FakeGitHub.received[0]["body"]["base"] == "main"


# ---------------------------------------------------------------------------
# 六、delivery 模块自身的单元测试（不需要 git push）
# ---------------------------------------------------------------------------


class TestDeliveryHelpers:
    def test_slugify(self):
        assert delivery.slugify("fix: 修导出乱码") == "fix"
        assert delivery.slugify("修复") == "change"
        assert delivery.slugify("") == "change"
        assert delivery.slugify("A" * 100) == "a" * 40

    def test_derive_branch_shape(self):
        b = delivery.derive_branch("fix: x", "abc1234")
        assert b == "agent/fix-x-abc1234"
        assert b.count("/") == 1

    def test_derive_branch_is_idempotent(self):
        """同一个标题 + 同一个 sha → 同一个分支名。重试才能复用分支。"""
        a = delivery.derive_branch("t", "sha1")
        b = delivery.derive_branch("t", "sha1")
        assert a == b

    def test_default_api_base_can_be_overridden(self, monkeypatch):
        monkeypatch.delenv("GITHUB_API_URL", raising=False)
        assert delivery.default_api_base() == "https://api.github.com"
        monkeypatch.setenv("GITHUB_API_URL", "https://ghe.corp/api/v3/")
        assert delivery.default_api_base() == "https://ghe.corp/api/v3"

    def test_split_command_strips_quotes(self):
        from mewcode.delivery import split_command

        got = split_command('"C:\\Program Files\\py.exe" -m pytest -q')
        assert got[0] == "C:\\Program Files\\py.exe"
        assert got[1:] == ["-m", "pytest", "-q"]

    def test_commit_all_reports_no_change(self, repo):
        changed, why = delivery.commit_all(repo, "nothing")
        assert changed is False
        assert "没有需要提交的改动" in why

    def test_commit_all_commits_when_dirty(self, repo):
        (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        changed, _ = delivery.commit_all(repo, "bump")
        assert changed is True
        assert git(repo, "rev-list", "--count", "HEAD").strip() == "2"

    def test_head_sha_and_current_branch(self, repo):
        assert delivery.current_branch(repo) == "main"
        assert len(delivery.head_sha(repo)) >= 7
        assert len(delivery.head_sha(repo, short=False)) == 40

    def test_checkout_branch_creates_and_reuses(self, repo):
        delivery.checkout_branch(repo, "agent/x-1")
        assert delivery.current_branch(repo) == "agent/x-1"
        # 再来一次不该失败（-B 的幂等性）
        delivery.checkout_branch(repo, "agent/x-1")
        assert delivery.current_branch(repo) == "agent/x-1"

    def test_checkout_branch_guards_too(self, repo):
        with pytest.raises(delivery.DeliveryError):
            delivery.checkout_branch(repo, "main")

"""开 PR 那条 HTTP 路径的测试。

## 为什么要有这个文件

`_open_pr` 之前把 `https://api.github.com` **写死**在代码里，于是：

  · GitHub Enterprise / Gitea 用户根本用不了
  · 这条路径**没法被测** —— 没有 token 就永远走不到，代码里那几十行
    网络+错误处理逻辑从写下第一天起就没真正执行过
  · 本机没有 `gh`、没有 `GITHUB_TOKEN`，所以"开 PR"这件事
    一直是"代码写了但没验过"

现在 `--api-base` / `GITHUB_API_URL` 可配置，于是可以起一个**本地假 GitHub API**，
让真实的 `urllib` 请求真的发出去，从而验证：

  · 请求 URL 拼得对不对（`{api_base}/repos/{owner}/{name}/pulls`）
  · 认证头和 Accept 头对不对
  · payload 里 title / head / base / body 对不对（**base 必须是基线分支**）
  · 401 / 400 之类的错误会不会被正确捕获并给出可读信息
  · 没有凭据时会不会给出"人能接手"的提示，而不是崩掉

    pytest tests/test_pr_open_http.py -v
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mewcode import delivery

_CI_SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "apply_and_open_pr.py"


def _load_ci_module():
    """从文件路径加载 ci 脚本（ci/ 不是包，不能用 import 语句）。

    `open_pr` 的实现已经移到 `mewcode/delivery.py`，由 CI 脚本和
    `tools/create_pr.py` 共用 —— 所以这里测的是那份共享实现，
    而不是某个脚本里的副本。
    """
    spec = importlib.util.spec_from_file_location("_ci_apply_open_pr", _CI_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ci = _load_ci_module()


# ---------------------------------------------------------------------------
# 假的 GitHub API
# ---------------------------------------------------------------------------


class _FakeGitHub(BaseHTTPRequestHandler):
    """只实现 POST /repos/{owner}/{repo}/pulls。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    response_status = 201
    response_body: dict = {}

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw.decode("utf-8"),
            }
        )
        body = json.dumps(self.response_body).encode()
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_github():
    _FakeGitHub.requests = []
    _FakeGitHub.response_status = 201
    _FakeGitHub.response_body = {"html_url": "https://github.com/acme/demo/pull/7"}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T")):
        r = subprocess.run(["git", *args], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    (d / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    for args in (("add", "-A"), ("commit", "-m", "init")):
        r = subprocess.run(["git", *args], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    return d


@pytest.fixture
def body_file(tmp_path: Path) -> Path:
    p = tmp_path / "pr.md"
    p.write_text("## 改了什么\n\n修了判空\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _no_gh_and_no_proxy(monkeypatch):
    """强制走 REST API 那条路，并绕开系统代理。

    · `shutil.which("gh")` 打桩成 None —— 否则装了 gh 的机器会走另一条分支，
      两条分支的实现完全不同，测试就测不到 API 代码了。
    · `no_proxy` 设上 —— httpx 那次踩过的坑（Windows 上代理从注册表读，
      连 127.0.0.1 都被塞进代理）对 urllib 一样可能发生。
    """
    import shutil

    monkeypatch.setattr(delivery.shutil, "which", lambda name: None)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


# ---------------------------------------------------------------------------
# 一、请求构造（核心）
# ---------------------------------------------------------------------------


class TestRequestConstruction:
    def test_successful_pr_creation(self, repo, body_file, fake_github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        r = subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/demo.git"],
            cwd=repo, capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

        ok, msg = delivery.open_pr(
            repo, "fix: 修判空", body_file, "main", "agent/fix-abc123",
            api_base=fake_github,
        )

        assert ok is True, msg
        assert "https://github.com/acme/demo/pull/7" in msg
        assert len(_FakeGitHub.requests) == 1

    def test_url_path_is_correct(self, repo, body_file, fake_github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)
        delivery.open_pr(repo, "t", body_file, "main", "agent/x", api_base=fake_github)

        path = _FakeGitHub.requests[0]["path"]
        assert path == "/repos/acme/demo/pulls", path

    def test_auth_and_accept_headers(self, repo, body_file, fake_github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)
        delivery.open_pr(repo, "t", body_file, "main", "agent/x", api_base=fake_github)

        headers = _FakeGitHub.requests[0]["headers"]
        assert headers["authorization"] == "Bearer ghp_test_token"
        assert headers["accept"] == "application/vnd.github+json"
        assert headers["content-type"] == "application/json"

    def test_payload_fields(self, repo, body_file, fake_github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)
        delivery.open_pr(repo, "fix: 修判空", body_file, "main", "agent/fix-abc123",
                    api_base=fake_github)

        payload = json.loads(_FakeGitHub.requests[0]["body"])
        assert payload["title"] == "fix: 修判空"
        assert payload["head"] == "agent/fix-abc123"
        # ★ base 必须是**基线分支**，不能是 agent 分支 —— 否则 PR 会自己合自己
        assert payload["base"] == "main"
        assert payload["head"] != payload["base"]
        assert "修了判空" in payload["body"]

    def test_ssh_remote_url_also_parsed(self, repo, body_file, fake_github, monkeypatch):
        """SSH 形式的远端地址（git@github.com:owner/repo.git）也要能解析。"""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "git@github.com:acme/ssh-repo.git"], cwd=repo,
                       capture_output=True, text=True)
        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                              api_base=fake_github)
        assert ok is True, msg
        assert _FakeGitHub.requests[0]["path"] == "/repos/acme/ssh-repo/pulls"

    def test_remote_url_without_dot_git(self, repo, body_file, fake_github, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/nodotgit"], cwd=repo,
                       capture_output=True, text=True)
        ok, _ = delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                            api_base=fake_github)
        assert ok is True
        assert _FakeGitHub.requests[0]["path"] == "/repos/acme/nodotgit/pulls"

    def test_env_var_api_base_is_used(self, repo, body_file, fake_github, monkeypatch):
        """不传 api_base 时应该读 GITHUB_API_URL（自建实例的接入口）。"""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        monkeypatch.setenv("GITHUB_API_URL", f"{fake_github}/api/v3")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is True, msg
        assert _FakeGitHub.requests[0]["path"] == "/api/v3/repos/acme/demo/pulls"

    def test_trailing_slash_in_api_base(self, repo, body_file, fake_github, monkeypatch):
        """api_base 末尾多一个斜杠不能拼出 `//repos/...`。"""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)
        delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                    api_base=f"{fake_github}/")
        assert _FakeGitHub.requests[0]["path"] == "/repos/acme/demo/pulls"


# ---------------------------------------------------------------------------
# 二、失败路径
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_http_error_is_reported_readably(self, repo, body_file, fake_github,
                                            monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "bad_token")
        _FakeGitHub.response_status = 401
        _FakeGitHub.response_body = {"message": "Bad credentials"}
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                              api_base=fake_github)
        assert ok is False
        assert "401" in msg
        assert "Bad credentials" in msg

    def test_validation_error_surfaces_github_message(self, repo, body_file,
                                                      fake_github, monkeypatch):
        """422：典型的"分支还没推上去就开 PR"。GitHub 的原因必须原样透出来。"""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        _FakeGitHub.response_status = 422
        _FakeGitHub.response_body = {"message": "Validation Failed",
                                     "errors": [{"message": "No commits between main and agent/x"}]}
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                              api_base=fake_github)
        assert ok is False
        assert "422" in msg
        assert "No commits between" in msg

    def test_no_token_and_no_gh_gives_actionable_message(self, repo, body_file,
                                                         monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is False
        assert "GITHUB_TOKEN" in msg  # 告诉人该怎么补

    def test_non_github_remote_gives_clear_reason(self, repo, body_file, monkeypatch):
        """远端不是 github.com 时，要说清是"地址解析不了"，而不是静默失败。"""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        subprocess.run(["git", "remote", "add", "origin",
                        "https://gitlab.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is False
        assert "owner/repo" in msg

    def test_missing_remote_gives_clear_reason(self, repo, body_file, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is False
        assert "owner/repo" in msg

    def test_failure_does_not_raise(self, repo, body_file, fake_github, monkeypatch):
        """无论 GitHub 怎么回，_open_pr 都不该抛异常 —— 它要能降级成人能接手。"""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        _FakeGitHub.response_status = 500
        _FakeGitHub.response_body = {"message": "Internal Error"}
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)
        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x",
                              api_base=fake_github)
        assert ok is False and isinstance(msg, str)


# ---------------------------------------------------------------------------
# 三、gh 优先
# ---------------------------------------------------------------------------


class TestGhPreference:
    def test_gh_is_preferred_when_present(self, repo, body_file, monkeypatch):
        """装了 gh 就用 gh（它能处理企业实例的各种认证），不再打 API。"""
        import shutil

        calls: list[list[str]] = []

        def fake_which(name):
            return "/usr/bin/gh" if name == "gh" else None

        monkeypatch.setattr(delivery.shutil, "which", fake_which)
        monkeypatch.setenv("GITHUB_TOKEN", "tok")

        def fake_run(cmd, cwd, timeout=None):
            calls.append(cmd)
            return 0, "https://github.com/acme/demo/pull/9"

        monkeypatch.setattr(delivery, "run", fake_run)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/demo.git"], cwd=repo,
                       capture_output=True, text=True)

        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is True
        assert calls and calls[0][0] == "gh"
        assert "--base" in calls[0] and "--head" in calls[0]

    def test_gh_failure_is_reported(self, repo, body_file, monkeypatch):
        monkeypatch.setattr(delivery.shutil, "which",
                            lambda name: "/usr/bin/gh" if name == "gh" else None)
        monkeypatch.setattr(delivery, "run", lambda cmd, cwd, timeout=None:
                            (1, "gh: not authenticated"))
        ok, msg = delivery.open_pr(repo, "t", body_file, "main", "agent/x")
        assert ok is False
        assert "not authenticated" in msg

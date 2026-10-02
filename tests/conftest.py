from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.retention.generator import build_case
from tests.retention.schema import ProbeCase

# 覆盖内置 tmp_path 时用的项目内目录，可随时整目录删除。
# 名字刻意避开 .pytest-tmp —— 见下方 tmp_path 的说明。
_TMP_ROOT = Path(__file__).resolve().parent.parent / ".eval-tmp"


def _safe_name(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid)[:80]


def _force_rmtree(root: Path) -> None:
    """删除目录树，遇到只读文件先解除只读再删。

    Windows 上 `git init` 会把 `.git/objects/**` 标成只读，普通的
    `shutil.rmtree` 删不动它们；配合 `ignore_errors=True` 会**静默留下半个目录**，
    下一次跑同一个用例时 `mkdir()` 就撞上 FileExistsError。
    """

    def _onexc(func, path, _exc):  # noqa: ANN001
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except OSError:
            pass

    if root.exists():
        shutil.rmtree(root, onexc=_onexc)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    r"""覆盖 pytest 内置实现。

    内置实现会转成 Windows 扩展长度路径（带 `\\?\` 前缀那种）来做长路径支持，
    而本机沙箱对该前缀的 scandir / rmtree 一律拒绝。这里换成普通的项目内短路径，
    语义等价且可写可删。

    另外因为不再请求内置的 `tmp_path_factory`，pytest 的 basetemp 始终为 None，
    session 结束时也不会去做 `cleanup_dead_symlinks`。

    ★ 每次先清空：pytest 内置语义就是「每个用例一个空目录」。不清的话第二次跑
    同一个用例时，上轮遗留的文件会让 `mkdir()` / `git init` 之类的操作直接报错。
    """
    d = _TMP_ROOT / _safe_name(request.node.nodeid)
    _force_rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    """给 auto_compact 用的 session 目录（它会 rmtree 后重建，必须可写）。"""
    d = tmp_path / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def outside_any_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一个**真的是**"不在任何 git 仓库里"的目录。

    ## 为什么需要它

    本仓库自己就是个 git checkout（`.git` 在仓库根），而 `tmp_path` 在
    `<仓库>/.eval-tmp/<用例名>` 下 —— 于是 `git rev-parse` 会一路往上
    找到仓库根的 `.git`，那个 `plain/` 就**不再**"不在仓库里"了。

    这不是某台机器的问题：**任何人 clone 之后跑测试都会撞上**。
    （实测：在仓库根 `git init` 之后，`test_not_a_git_repo` 和
    `test_not_a_git_repo_fails` 立刻从通过变成失败。）

    ## 解法

    `GIT_CEILING_DIRECTORIES` 是 git 自带的机制：列在里面的目录**之上**
    不再向上搜索仓库。把它指向 `tmp_path`，搜索就止步于这一层。
    （在 Windows 上这一条只要一个目录，不涉及 `;` 分隔的问题。）
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    d = tmp_path / "plain"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def prefix_case() -> ProbeCase:
    """探针埋在 prefix 区（会被摘要掉）——诊断摘要模板的主战场。"""
    return build_case("fx-prefix", target_tokens=8_000)


@pytest.fixture(scope="session")
def keepzone_case() -> ProbeCase:
    """探针埋在 keep 区（尾部原文）——验证排除法分支。"""
    return build_case(
        "fx-keepzone",
        target_tokens=12_000,
        with_trap=False,
        probes_in_tail=True,
    )


# ---------------------------------------------------------------------------
# 控制组：本环境到底能不能真实 git push
# ---------------------------------------------------------------------------

#: 负向断言空过时的统一说明文案
PUSH_SKIP_REASON = (
    "本环境无法完成真实 `git push`，所以「远端没有被改动」这类断言**无法区分**"
    "「没有推送」和「尝试推送但失败了」—— 它们会空过，一律跳过而不是假装通过。\n"
    "常见原因：沙箱禁止创建具名管道，Git for Windows 的 sh.exe 因此建不了 signal pipe "
    "（`fatal error - couldn't create signal pipe, Win32 error 5`）。\n"
    "这些断言在能推送的环境（CI、开发机）里会真实执行。"
)


def _try_real_push(root: Path) -> tuple[bool, str]:
    """真的建一个 bare 远端并推一次。返回 (是否成功, 说明)。"""
    origin = root / "origin.git"
    work = root / "work"
    origin.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    try:
        if run(origin, "init", "--bare", "-b", "main").returncode != 0:
            return False, "git init --bare 失败"
        run(work, "init", "-b", "main")
        run(work, "config", "user.email", "probe@example.com")
        run(work, "config", "user.name", "Probe")
        (work / "probe.txt").write_text("probe\n", encoding="utf-8")
        run(work, "add", "-A")
        if run(work, "commit", "-m", "probe").returncode != 0:
            return False, "git commit 失败"
        run(work, "remote", "add", "origin", str(origin))
        r = run(work, "push", "-u", "origin", "main")
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[:300]
        # 再确认远端真的多了一个 commit（push 返回 0 但没上去也算失败）
        count = run(origin, "rev-list", "--all", "--count").stdout.strip()
        if count != "1":
            return False, f"push 返回 0 但远端 commit 数是 {count!r}（期望 '1'）"
        return True, "ok"
    except OSError as e:  # git 不存在之类
        return False, f"{type(e).__name__}: {e}"


@pytest.fixture(scope="session")
def real_push_supported(tmp_path_factory) -> bool:
    """控制组：证明**本环境里 push 真的能成功**。

    为什么必须有它：

    `test_origin_untouched_after_create_pr` 断言"远端 commit 数没变"。
    如果 push 在这个环境里根本跑不通，"CreatePR 没有推送"和
    "CreatePR 尝试推送但失败了"在断言上**完全无法区分** —— 测试会空过，
    而且看起来是绿的。

    这和 retention 评测里"控制组必须 100%"是同一条原则：
    **没有先证明机制本身可用，负向断言就没有判别力。**

    这里真的推一次；推不上去就返回 False，请求它的测试会 skip 而不是通过。
    """
    root = _TMP_ROOT / "_push_probe"
    _force_rmtree(root)
    ok, detail = _try_real_push(root)
    if not ok:
        print(f"\n[控制组] 本环境真实 git push 不可用：{detail}")
    return ok


@pytest.fixture
def requires_real_push(real_push_supported: bool) -> None:
    """给"远端没被改动"这类**负向**断言用的守卫。

    ⚠️ 注意：控制组本身也有一条可见的测试
    （`tests/test_create_pr.py::test_control_real_push_works`），
    因为 conftest.py **不是测试模块，里面的 test_ 函数不会被收集** ——
    只把控制组写在这里的话，它会静默地永远不运行。
    """
    if not real_push_supported:
        pytest.skip(PUSH_SKIP_REASON)

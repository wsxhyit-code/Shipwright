"""验证门禁 demo：跑一遍完整的"改代码 → 验证 → 产出补丁"，并证明它不推送。

    python docs/devops-agent/demo_verify_gate.py

用真实 git 仓库 + 真实 pytest 退出码，不依赖网络、不依赖 API。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mewcode.tools.create_pr import CreatePRTool, make_verifier  # noqa: E402

DEMO = Path(__file__).resolve().parent / ".demo-repo"
ORIGIN = Path(__file__).resolve().parent / ".demo-origin.git"


def sh(cwd: Path | None, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败:\n{r.stderr}")
    return r.stdout.strip()


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def force_rmtree(root: Path) -> None:
    """删除目录树，遇到只读文件先解除只读。

    Windows 上 `git init` 会把 `.git/objects/**` 标成只读，普通 rmtree
    删不动；配合 ignore_errors 会静默留下半个目录，导致下次 `mkdir` 撞车。
    """

    def _onexc(func, path, _exc):  # noqa: ANN001
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except OSError:
            pass

    if root.exists():
        shutil.rmtree(root, onexc=_onexc)


def setup() -> None:
    for p in (DEMO, ORIGIN):
        force_rmtree(p)
    ORIGIN.mkdir(parents=True)
    sh(ORIGIN, "init", "--bare", "-b", "main")

    DEMO.mkdir(parents=True)
    sh(DEMO, "init", "-b", "main")
    sh(DEMO, "config", "user.email", "demo@example.com")
    sh(DEMO, "config", "user.name", "Demo")
    sh(DEMO, "remote", "add", "origin", str(ORIGIN))

    (DEMO / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (DEMO / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    sh(DEMO, "add", "-A")
    sh(DEMO, "commit", "-m", "init")
    # 注意：**刻意不预先 push**。
    #   ① demo 的证明点是"origin 上的 commit 数不会增加"，起始为 0 反而更干净；
    #   ② 本机沙箱禁止创建具名管道，git 的 push 会经 sh.exe 而起不来
    #      （`couldn't create signal pipe, Win32 error 5`）。你在自己机器上跑时
    #      可以补上 `git push -u origin main`。


def make_tool() -> CreatePRTool:
    cmd = f'"{sys.executable}" -m pytest test_calc.py -q'
    return CreatePRTool(
        work_dir=str(DEMO),
        verify_command=cmd,
        artifacts_dir=str(DEMO / ".mewcode" / "pr"),
        base_ref="main",
        timeout=120,
        verifier=make_verifier(cmd, str(DEMO), 120),
    )


async def call(tool: CreatePRTool, **kw):
    return await tool.execute(tool.params_model.model_validate(kw))


def show_gate(verify_cmd: str) -> None:
    print(f"  验证命令：{verify_cmd}")


async def main() -> None:
    banner("准备：一个有测试的 git 仓库（origin 是 bare 仓库，用来验证「没被推送」）")
    setup()
    print(f"  仓库：{DEMO}")
    print(f"  远端：{ORIGIN}")
    print(f"  origin 上的 commit 数：{sh(ORIGIN, 'rev-list', '--all', '--count')}  （起始值，全程不应变化）")
    tool = make_tool()

    # ---- 场景 1：代码改坏了 -------------------------------------------
    banner("场景 1：把 add 改成减法（模拟「改坏了」）→ 尝试提 PR")
    (DEMO / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    print("  calc.py 已改成：return a - b        ← 测试必然失败\n")

    r = await call(tool, title="fix: 修 add", issue="#142")
    print(f"  CreatePR 返回 is_error = {r.is_error}")
    print("  返回内容：")
    for line in r.output.splitlines():
        print(f"    │ {line}")

    patch = DEMO / ".mewcode" / "pr" / "changes.patch"
    print(f"\n  补丁文件是否生成：{'是 ❌' if patch.exists() else '否 ✅（验证没过就不该产出）'}")

    # ---- 场景 2：改好了 ----------------------------------------------
    banner("场景 2：把代码改对 → 再次尝试提 PR")
    (DEMO / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # 修好了\n", encoding="utf-8")
    print("  calc.py 已改回：return a + b\n")

    r = await call(tool, title="fix: 修 add", description="加回正确的加法实现", issue="#142")
    print(f"  CreatePR 返回 is_error = {r.is_error}")
    print("  返回内容：")
    for line in r.output.splitlines():
        print(f"    │ {line}")

    # ---- 证明：没有推送 ----------------------------------------------
    banner("证明：远端一个 commit 都没多（AI 没有推送权限）")
    print(f"  origin 上的 commit 数：{sh(ORIGIN, 'rev-list', '--all', '--count')}  （起始为 0，跑完仍是 0）")
    branches = sh(ORIGIN, "branch", "--list")
    print(f"  origin 上的分支：{branches or '(空)'}")
    print(f"  本地分支：{sh(DEMO, 'branch', '--list')}")
    print("\n  → 补丁只在本地产出，推送必须由 CI 的确定性脚本完成")

    # ---- 展示产物 ----------------------------------------------------
    banner("产物：CI 拿到的两个文件")
    print(f"  {patch.relative_to(DEMO)}：")
    for line in patch.read_text(encoding="utf-8").splitlines()[:12]:
        print(f"    │ {line}")
    print(f"\n  {(DEMO / '.mewcode' / 'pr' / 'pr.json').relative_to(DEMO)}：")
    for line in (DEMO / ".mewcode" / "pr" / "pr.json").read_text(
        encoding="utf-8"
    ).splitlines():
        print(f"    │ {line}")

    banner("清理 demo 目录")
    shutil.rmtree(DEMO, ignore_errors=True)
    shutil.rmtree(ORIGIN, ignore_errors=True)
    print("  已清理")


if __name__ == "__main__":
    asyncio.run(main())

"""独立验证：agent 交出来的补丁到底有没有真的修好。

## 为什么要单独做这一步

`CreatePR` 内部已经跑过一次验证（退出码 0）和一次独立验证（VERDICT: PASS），
但那两次都发生在**改代码的那个工作区**里，而且那个独立验证者自己承认：

    「本环境没有 Bash/执行工具，我无法实际运行 python -m pytest -q；
      以上结论来自静态追踪和测试代码检查。」

静态推理不是执行。这个脚本做的是**干净副本上的重放**，和 CI 收尾脚本
（ci/apply_and_open_pr.py）的第一步同构：

    ① 从基线导出干净副本（不是当前工作区 —— 那里躺着被测对象的改动）
    ② 应用补丁
    ③ **对账**：打出来的文件内容，是否等于补丁头里记的后像 blob 哈希
    ④ 在副本里重跑验收命令（不看 agent 的转述）
    ⑤ 负向控制：把 agent 新增的回归测试放到**未修复**的代码上跑，必须失败

## 两个"空过"的坑，都是实测踩到的

**坑一：`git apply` 会"跳过"补丁并返回退出码 0。**

    $ git -C <copy> apply -v -p1 changes.patch
    Skipped patch 'src/account_api/policy.py'.
    Skipped patch 'tests/test_account_summary.py'.
    $ echo $LASTEXITCODE
    0

文件一个字没改，退出码却是 0，`pytest -q` 也照样绿（基线本来就绿）——
于是「补丁生效了」和「补丁根本没打上」在输出上完全一样。
所以这里**不用 git apply**：自己按 unified diff 应用（纯标准库，20 行），
再用补丁头里的 `index <old>..<new>` 后像哈希对账。改了就是改了，哈希说了算。

**坑二：`pytest -k <name>` 在没匹配到用例时以退出码 5 结束。**

    5 != 0 → 「测试真的红了」和「测试根本没跑」看起来一模一样。
    所以负向控制里先 `--collect-only` 确认那条测试确实被收集到了。

用法（先建好两个工作目录，见 _ensure_dirs）：

    python eval/shipwright-triage/verify_patch.py
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "account-api"
ARTIFACTS = APP / ".mewcode" / "pr"

VERIFY_CMD = [r"D:\.venv\Scripts\python.exe", "-m", "pytest", "-q"]
BASE_REF = "main"

WORK_ROOT = HERE / ".verify-work"
FIXED_DIR = WORK_ROOT / "fixed"
BUGGY_DIR = WORK_ROOT / "buggy"


# ---------------------------------------------------------------------------
# 一、先把补丁拆开，并把它作为一个"规范"用起来
# ---------------------------------------------------------------------------


def blob_sha(data: bytes) -> str:
    """算 git blob 哈希：`sha1("blob <len>\\0" + content)`。

    拿它和补丁头里的后像哈希对账 —— 这是"补丁到底打上没有"的机械证据，
    不依赖任何命令的退出码。
    """
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def split_patch(patch: str) -> list[dict]:
    """把补丁拆成 [{path, before_sha, after_sha, hunks}]。"""
    files: list[dict] = []
    current: dict | None = None
    lines = patch.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = {"path": None, "before_sha": None, "after_sha": None, "hunks": []}
            files.append(current)
        elif line.startswith("index ") and current is not None:
            spec = line.split()[1]
            if ".." in spec:
                before, after = spec.split("..")
                current["before_sha"], current["after_sha"] = before, after
        elif line.startswith("+++ ") and current is not None:
            current["path"] = line[4:].strip()
            if current["path"].startswith("b/"):
                current["path"] = current["path"][2:]
        elif line.startswith("@@") and current is not None:
            m = re.match(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line
            )
            if m is None:
                raise ValueError(f"看不懂的 hunk 头：{line!r}")
            old_start = int(m.group(1))
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@", "diff --git ")):
                body.append(lines[i])
                i += 1
            current["hunks"].append((old_start, "".join(body)))
            continue
        i += 1
    return [f for f in files if f["path"] and f["hunks"]]


def apply_unified_diff(original: bytes, hunks: list[tuple[int, str]]) -> bytes:
    """按 unified diff 把 hunks 应用到原始内容上。

    只认 ` ` / `-` / `+` 三种前缀，`\\ No newline at end of file` 忽略。
    行尾统一用 `\\n`（补丁本身是 LF；应用后再按原文件风格写回也无所谓，
    Python 不在乎 LF/CRLF，而 blob 哈希对账用的就是这里产出的字节）。
    """
    src = original.decode("utf-8").splitlines(keepends=True)
    out: list[str] = []
    pos = 0  # 已经原样搬过去的行数（0-based 游标）

    for old_start, body in sorted(hunks, key=lambda h: h[0]):
        start = old_start - 1
        out.extend(src[pos:start])
        pos = start
        for line in body.splitlines(keepends=True):
            if line.startswith("\\"):
                continue
            tag, content = line[0], line[1:]
            if tag == " ":
                out.append(content)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(content)
            elif line.strip() == "":
                # hunk 尾部常见的空行，忽略
                continue
            else:
                raise ValueError(f"看不懂的 hunk 行：{line!r}")
    out.extend(src[pos:])
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------------------
# 二、跑命令 / 准备副本
# ---------------------------------------------------------------------------


def run(argv: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _ensure_dirs() -> None:
    """确保两个工作目录存在。

    ⚠️ 沙箱行为：脚本自己新建的目录里再建子目录会被拒
    （`PermissionError: [WinError 5]`），往**已存在**的目录里写文件则允许。
    所以这两个目录由命令行先建好。
    """
    missing = [d for d in (FIXED_DIR, BUGGY_DIR) if not d.exists()]
    if missing:
        print("缺少工作目录，请先建好：")
        for d in missing:
            print(f"  New-Item -ItemType Directory -Force -Path '{d}'")
        raise SystemExit(2)


def export_baseline(dest: Path) -> dict[str, bytes]:
    """把基线导出到 dest，返回 {相对路径: 内容}。

    ⚠️ 不用 `git clone`：默认沙箱下 Git for Windows 的 `sh.exe` 建不了信号
    管道，连本地路径 clone 都跑不起来。`git show <ref>:<path>` 逐文件导出绕开它。
    """
    code, out = run(["git", "ls-tree", "-r", "--name-only", BASE_REF], APP)
    if code != 0:
        print(f"列基线文件失败：{out}")
        raise SystemExit(2)

    contents: dict[str, bytes] = {}
    for rel in (p for p in out.splitlines() if p.strip()):
        proc = subprocess.run(
            ["git", "show", f"{BASE_REF}:{rel}"],
            cwd=str(APP), capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            print(f"取 {rel} 失败")
            raise SystemExit(2)
        contents[rel] = proc.stdout
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(proc.stdout)

    print(f"  导出 {len(contents)} 个文件 → {dest}")
    return contents


def main() -> int:
    patch_file = ARTIFACTS / "changes.patch"
    if not patch_file.exists():
        print(f"找不到补丁：{patch_file}")
        return 2
    patch = patch_file.read_text(encoding="utf-8")

    _ensure_dirs()
    for d in (FIXED_DIR, BUGGY_DIR):
        for child in list(d.iterdir()):
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    failures = 0

    # ---------------------------------------------------------------- ①
    section("① 从基线导出干净副本（刻意不用当前工作区）")
    code, out = run(["git", "log", "--oneline", "-1", BASE_REF], APP)
    print(f"  基线 = {out.strip()}")
    fixed = export_baseline(FIXED_DIR)

    # ---------------------------------------------------------------- ②
    section("② 拆补丁：涉及哪些文件、期望的前像/后像哈希")
    files = split_patch(patch)
    for f in files:
        print(f"  {f['path']}")
        print(f"     前像 {f['before_sha']}  后像 {f['after_sha']}  "
              f"{len(f['hunks'])} 个 hunk")

    # ---------------------------------------------------------------- ③
    section("③ 应用补丁 + 对账（改了就是改了，哈希说了算）")
    for f in files:
        rel = f["path"]
        original = fixed.get(rel)
        if original is None:
            print(f"  ❌ 补丁里的 {rel} 在基线里不存在")
            failures += 1
            continue

        # git 的 `index` 行写的是**简写**哈希（7 位），拿它跟完整 SHA-1 比
        # 永远不相等 —— 所以只比前缀，两边都截到简写长度。
        short = len(f["before_sha"])
        got_before = blob_sha(original)
        if not got_before.startswith(f["before_sha"]):
            print(f"  ⚠️ {rel} 前像对不上：基线 {got_before[:short]} "
                  f"vs 补丁 {f['before_sha']}（补丁可能是对另一个版本做的）")
        else:
            print(f"  ✅ {rel} 前像一致 {got_before[:short]}")

        patched = apply_unified_diff(original, f["hunks"])
        got_after = blob_sha(patched)
        ok = got_after.startswith(f["after_sha"])
        print(f"  {'✅' if ok else '❌'} {rel}  应用后 {got_after[:short]} "
              f"(补丁记的后像 {f['after_sha']})")
        if not ok:
            failures += 1

        (FIXED_DIR / rel).write_bytes(patched)
        fixed[rel] = patched

    # ---------------------------------------------------------------- ④
    section("④ 在干净副本里重跑验收命令")
    print(f"  命令：{' '.join(VERIFY_CMD)}")
    print(f"  目录：{FIXED_DIR}")
    code, out = run(VERIFY_CMD, FIXED_DIR)
    print(f"  退出码 {code}")
    print("  " + "\n  ".join(out.strip().splitlines()[-5:]))
    if code != 0:
        failures += 1
    print(f"\n  → {'✅ 修复后的代码验收通过' if code == 0 else '❌ 验收失败'}")

    # ---------------------------------------------------------------- ⑤
    section("⑤ 负向控制：新增的回归测试在**未修复**代码上必须失败")
    added = re.findall(r"^\+\s{4}def (test_\w+)", patch, re.M)
    print(f"  补丁里新增的测试函数：{added or '(没有)'}")
    if not added:
        print("  ⚠️ agent 没有新增回归测试 —— 无法证明这条测试有判别力")
        failures += 1
    else:
        # ⚠️ 顺序要紧：先 export_baseline（它会把基线写一遍），
        # 再覆盖测试文件。反过来的话 export_baseline 会把刚放进去的新测试**盖掉** ——
        # 第一版就是这么错的，而现象是"对照组里没有那条新测试"，很容易误判成补丁问题。
        buggy = export_baseline(BUGGY_DIR)

        # 只把 agent 改过的**测试文件**搬过去；policy.py 保持基线（未修复），
        # 于是这份副本 = 旧实现 + 新测试 —— 正好是判断"这条测试有没有判别力"的对照组。
        for f in files:
            rel = f["path"]
            if "test" not in Path(rel).name:
                continue
            (BUGGY_DIR / rel).write_bytes(fixed[rel])
            print(f"  放入新测试：{rel}  （{len(fixed[rel])} 字节）")
        print(f"  实现文件保持基线（未修复）：{[p for p in buggy if p.endswith('policy.py')]}")

        # ⚠️ 不要拿两个副本的用例**总数**做判据。实测：基线文件里 5 个 def
        # （其中一个 parametrize 展开成 5 条）——`--collect-only -q` 在基线和
        # 修复后都报同样的条数，靠"总数 +1"判断新测试有没有进来是错的。
        # 直接问 pytest "这条测试在不在" 才是准的：
        #   · 在 → 执行它，退出码 1（真的红了）
        #   · 不在 → 退出码 5
        # 而 5 必须单独判掉 —— 否则"测试红了"和"测试根本没跑"看起来一样。
        code, out = run(
            [*VERIFY_CMD, "-k", added[0], "tests/test_account_summary.py"], BUGGY_DIR
        )
        print(f"  在对照组（旧实现 + 新测试）上跑 {added[0]} → 退出码 {code}")
        if code == 5:
            print("  ❌ 没收集到用例（退出码 5）—— 新测试没进对照组，结论不算数")
            failures += 1
        elif code == 0:
            print("  ❌ 新测试在未修复代码上仍然通过（退出码 0）—— 判别力不足")
            failures += 1
        elif code == 1:
            print("  ✅ 新测试在未修复代码上失败（退出码 1）—— 它真的在测这个 bug")
        else:
            print(f"  ❌ 非预期的退出码 {code}（既不是 0/1/5）")
            failures += 1
        print("  " + "\n  ".join(out.strip().splitlines()[-4:]))

        # 对照组上"其余用例是否仍然全绿"不能拿整份文件跑：文件里现在**包含**
        # 那条新测试，它必然红，于是这条检查会永远失败（第一版就是这样，
        # 现象是"基线就不干净"）。正确做法是显式排除新增的那条。
        code_rest, out_rest = run(
            [*VERIFY_CMD, "-q", "tests/test_account_summary.py", "-k", f"not {added[0]}"],
            BUGGY_DIR,
        )
        print(
            f"  同一份副本上【排除新测试后】的其余用例 → 退出码 {code_rest} "
            f"{'✅ 全绿（基线本身是好的，红的是新测试）' if code_rest == 0 else '⚠️ 基线就不干净'}"
        )
        print("  " + "\n  ".join(out_rest.strip().splitlines()[-2:]))
        if code_rest != 0:
            failures += 1

    section("结论")
    if failures:
        print(f"❌ 有 {failures} 项没通过 —— 见上面每一条")
        return 1
    print("✅ 补丁应用后与补丁头记录的后像哈希一致（内容真的变了）")
    print("✅ 修复后的代码在干净副本上验收通过（退出码 0）")
    print("✅ 新增的回归测试在未修复代码上确实会红（有判别力）")
    print("   agent 自己跑的那次验证 + 独立验证者的静态 PASS，现在有了第三个独立证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

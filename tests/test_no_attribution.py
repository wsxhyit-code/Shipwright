"""守卫：仓库里不允许出现署名 / 来源标注 / 平台推广这类字眼。

## 为什么要有这条测试

这一类文字以前以「文件头的 4 行注解块」的形式存在于 **192 个文件**里。
靠人工清理是清不干净的 —— 实测就漏过两次：

  · 3 个文件开头有**不可见的 UTF-8 BOM**，逐字比对第一行时失配
  · 某个文件第 4 行是**拼错的变体**，严格匹配会漏

而它们还会**从新写的文件里长回来** —— agent 每次新建文件都可能顺手加一行来源标注。
所以只有一条会失败的测试才靠得住。

## ⚠️ 这个文件里的禁止词是 hex 码点，不是字面量

因为要求是「仓库里一处都不许出现那些字」。如果这里直接写出字面量，
这个文件自己就会成为唯一一处。解码见 `_decode()`。

## 扫描范围

工作树里除以下之外的所有文本文件：

  · `.venv` / `.git` / `__pycache__` / `*.egg-info`      —— 不是我们的内容
  · `.eval-tmp` / `pytest-cache-files-*`                 —— 测试临时目录
  · `.revert-backup/`                                    —— 手工备份，不是仓库内容
  · `.mewcode/sessions` `.mewcode/session` 等运行时数据   —— 会话日志，不进仓库
  · 本文件自身                                           —— 它必须能识别那些词

## 为什么不禁「来源：」「出处：」

那两个字在正常中文里太常见了 —— 技术文档里写「这是……的直接来源：」
会被误伤（实测踩到过）。所以清单里只放**指向具体平台/个人/营销**的词。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

#: 禁止词（hex 码点）-> 说明。保持精简，只放「指向具体来源」的那些。
BANNED_HEX: list[tuple[str, str]] = [
    ("e5b08fe69e97", "个人署名"),
    ("7869616f6c696e", "来源站点域名"),
    ("e585abe882a1", "营销词"),
    ("e585ace4bc97e58fb7", "平台推广"),
    ("e7ae80e58e86e6a8a1e78988", "营销词"),
    ("e7ae80e58e86e6a8a1e69dbf", "营销词"),
    ("6a69616e6c692e", "来源站点域名"),
]


def _decode(h: str) -> str:
    """把 hex 码点还原成词。"""
    return bytes.fromhex(h).decode("utf-8")


#: 解码后的清单，供扫描用
BANNED: list[tuple[str, str]] = [(re.escape(_decode(h)), why) for h, why in BANNED_HEX]

#: 目录跳过规则。**必须用 (?:^|[\\/]) 锚定开头** ——
#: `rglob` 给出的顶层目录没有前导分隔符，只写 `[\\/]xxx[\\/]` 匹配不上，
#: 会把 `.revert-backup`、`.eval-tmp` 这类目录也扫进来（实测踩过）。
SKIP = re.compile(
    r"(?:^|[\\/])(?:\.venv|\.git|__pycache__|\.eval-tmp|\.revert-backup|\.pytest-tmp)(?:[\\/]|$)"
    r"|pytest-cache-files"
    r"|\.egg-info(?:[\\/]|$)"
    r"|(?:^|[\\/])\.mewcode[\\/](?:sessions?|file-history|history)(?:[\\/]|$)"
)

BINARY_SUFFIX = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
    ".whl", ".pyc", ".so", ".dll", ".exe", ".db", ".sqlite3",
}

SELF = pathlib.Path(__file__).resolve()


def _scan() -> list[str]:
    offenders: list[str] = []
    for p in sorted(REPO.rglob("*")):
        if not p.is_file():
            continue
        if p.resolve() == SELF:
            continue
        rel = p.relative_to(REPO)
        if SKIP.search(str(rel)):
            continue
        if p.suffix.lower() in BINARY_SUFFIX:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "\x00" in text[:4000]:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat, why in BANNED:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    offenders.append(
                        f"{rel}:{i}  [{why}] 命中 {m.group(0)!r}：{line.strip()[:80]}"
                    )
    return offenders


def test_no_attribution_or_promo_text_in_repo():
    """★ 仓库里任何文件都不许出现署名 / 来源标注 / 平台推广。

    失败时下面会列出**具体文件、行号、命中的词**。
    修法是删掉那段文字（通常是文件头的注解块），而不是改这条测试。
    """
    offenders = _scan()
    if offenders:
        detail = "\n".join("  " + o for o in offenders)
        pytest.fail(
            f"仓库里发现 {len(offenders)} 处署名/推广类文字：\n{detail}\n\n"
            "请把对应文字删掉。这类内容不应出现在任何文件里"
            "（包括注释头、docstring、markdown、配置文件）。"
        )


def test_the_guard_itself_would_catch_a_known_offender():
    """反向控制：证明这条守卫**真的会失败**，而不是永远返回空。

    没有这一条的话，`_scan()` 里一个写错的正则会让守卫永远绿 ——
    那种"看起来有护栏、其实没有"的状态比没有护栏更危险。
    """
    probe = REPO / "tests" / "_guard_probe_test_file.txt"
    try:
        probe.write_text(_decode("2320e69da5e6ba90efbc9ae585ace4bc97e58fb740e5b08fe69e97636f64696e67"), encoding="utf-8")
        offenders = _scan()
        assert any("_guard_probe_test_file.txt" in o for o in offenders), (
            "守卫没抓到明显违规的文件 —— 正则或跳过规则写错了"
        )
    finally:
        probe.unlink(missing_ok=True)


def test_skip_rules_actually_skip():
    """控制：被跳过的目录里放违规内容**不该**被判违规。

    否则守卫会因为 `.eval-tmp` / `.revert-backup` 里的历史内容一直红，
    变成一个没人看的测试。
    """
    probe = REPO / ".eval-tmp" / "_skip_probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    try:
        probe.write_text(_decode("2320e69da5e6ba90efbc9ae585ace4bc97e58fb740e5b08fe69e97636f64696e67"), encoding="utf-8")
        assert not any("_skip_probe.txt" in o for o in _scan())
    finally:
        probe.unlink(missing_ok=True)

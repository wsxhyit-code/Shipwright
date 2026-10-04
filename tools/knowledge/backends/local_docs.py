"""本地文档后端：把「一堆 markdown 文档」变成可检索的知识库。

## 为什么需要它

企业知识库不一定有 API。更常见的情况是：规范就放在代码仓库里
（`docs/standards/*.md`、`CONTRIBUTING.md`、`docs/adr/*.md`），
或者放在一份共享目录里。

这个后端让**不需要任何外部系统**就能用起来：

    <工作目录>/.mewcode/knowledge/     ← 项目级
    ~/.mewcode/knowledge/              ← 用户级（跨项目共用）
    外加配置里 extra_dirs 指定的目录

目录约定和 `skills/`、`agents/` 保持一致（项目级 + 用户级），
不引入新的心智负担。

## 两个实现细节

**① 按 mtime 增量读，不每次全读。**
搜索需要读正文才能打分。文档多了以后每次搜索都全量读盘会很慢，
所以缓存 `路径 -> (mtime, size, title, tags, body)`，只重读变过的文件。
（agent 在工作过程中新写的规范文档也能立刻被搜到。）

**② 有硬上限。**
单文件超过 `MAX_DOC_BYTES` 会被截断读取并标注；文档总数也有上限。
知识库目录里塞进一个巨大的日志文件不应该把 agent 拖死。
"""
from __future__ import annotations

import pathlib
import re
from typing import Any

from mewcode.tools.knowledge.backend import (
    MIN_SCORE,
    KnowledgeDoc,
    KnowledgeError,
    KnowledgeHit,
    build_idf,
    make_snippet,
    min_matches_for,
    score_and_match,
    tokenize,
)

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}

#: 单篇文档最多读多少字节（超过就截断，并在正文里留标记）
MAX_DOC_BYTES = 512 * 1024

#: 最多收录多少篇（防止有人误把大目录指进来）
MAX_DOCS = 2000

_PROJECT_DIR = ".mewcode/knowledge"
_USER_DIR = "~/.mewcode/knowledge"

_TAGS_RE = re.compile(r"^\s*(?:tags|标签)\s*[:：]\s*(.+)$", re.MULTILINE)
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _doc_id_for(root: pathlib.Path, path: pathlib.Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return "/".join(rel.parts)


def _extract_title(body: str, fallback: str) -> str:
    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    # 没有一级标题就用文件名，把连字符/下划线换成空格
    return fallback.replace("-", " ").replace("_", " ").strip()


def _extract_tags(body: str) -> list[str]:
    m = _TAGS_RE.search(body)
    if not m:
        return []
    raw = m.group(1)
    parts = re.split(r"[,，、\s]+", raw)
    return [p.strip() for p in parts if p.strip()][:12]


class LocalDocsBackend:
    """扫描本地目录里的文本文档，做关键词检索。"""

    def __init__(
        self,
        work_dir: str | pathlib.Path,
        extra_dirs: list[str] | None = None,
        include_user_dir: bool = True,
    ) -> None:
        self._work_dir = pathlib.Path(work_dir).resolve()
        self._roots: list[tuple[pathlib.Path, str]] = []

        project = self._work_dir / _PROJECT_DIR
        if project.is_dir():
            self._roots.append((project, "project"))

        if include_user_dir:
            user = pathlib.Path(_USER_DIR).expanduser()
            if user.is_dir() and user.resolve() != project.resolve():
                self._roots.append((user, "user"))

        for d in extra_dirs or []:
            p = pathlib.Path(d)
            if not p.is_absolute():
                p = self._work_dir / p
            if p.is_dir():
                self._roots.append((p.resolve(), "extra"))

        # 路径 -> (mtime_ns, size, KnowledgeDoc, body)
        self._cache: dict[pathlib.Path, tuple[int, int, KnowledgeDoc, str]] = {}

    # --- 扫描 ---

    def _iter_files(self) -> list[tuple[pathlib.Path, str]]:
        found: list[tuple[pathlib.Path, str]] = []
        for root, source in self._roots:
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if any(part.startswith(".") and part != "." for part in p.parts):
                    # 跳过隐藏目录/文件（.git、.obsidian 之类）
                    if any(part.startswith(".") for part in p.relative_to(root).parts):
                        continue
                found.append((p, source))
                if len(found) >= MAX_DOCS:
                    return found
        return found

    def _load(self, path: pathlib.Path, source: str) -> tuple[KnowledgeDoc, str] | None:
        try:
            st = path.stat()
        except OSError:
            return None

        cached = self._cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2], cached[3]

        try:
            raw = path.read_bytes()[:MAX_DOC_BYTES]
            body = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        truncated = st.st_size > MAX_DOC_BYTES
        if truncated:
            body += f"\n\n…（文件超过 {MAX_DOC_BYTES} 字节，已截断）"

        # doc_id 用「相对哪个 root」算
        doc_id = ""
        for root, _src in self._roots:
            try:
                doc_id = _doc_id_for(root, path)
                break
            except ValueError:
                continue
        if not doc_id:
            doc_id = path.stem

        doc = KnowledgeDoc(
            doc_id=doc_id,
            title=_extract_title(body, path.stem),
            source=source,
            location=str(path),
            tags=_extract_tags(body),
            updated=_fmt_time(st.st_mtime),
        )
        self._cache[path] = (st.st_mtime_ns, st.st_size, doc, body)
        return doc, body

    def _all(self) -> list[tuple[KnowledgeDoc, str]]:
        out: list[tuple[KnowledgeDoc, str]] = []
        for path, source in self._iter_files():
            loaded = self._load(path, source)
            if loaded:
                out.append(loaded)
        return out

    # --- KnowledgeBackend ---

    def search(self, query: str, limit: int = 5) -> list[KnowledgeHit]:
        qt = tokenize(query)
        if not qt:
            return []
        corpus = self._all()
        # 先按整个语料算 IDF —— 到处都出现的词（"存在""问题""的话"）几乎不参与打分。
        #
        # 不做这一步的后果是实测出来的：用「zzz不存在的话题」这种**无意义查询**
        # 也能命中 2 条看起来很像样的文档，而 agent 很可能拿其中一条当真。
        idf = build_idf(
            [tokenize(f"{d.title} {' '.join(d.tags)} {b}") for d, b in corpus]
        )
        scored: list[tuple[float, KnowledgeDoc, str]] = []
        need = min_matches_for(qt)
        for doc, body in corpus:
            sb = score_and_match(
                query, title=doc.title, tags=doc.tags, body=body, idf=idf
            )
            # 两个条件都要过：分数够高**且**覆盖度够 —— 见 min_matches_for 的说明
            if sb.score >= MIN_SCORE and sb.n_matched >= need:
                scored.append((sb.score, doc, body))
        scored.sort(key=lambda x: (-x[0], x[1].doc_id))

        return [
            KnowledgeHit(doc=doc, score=round(s, 4), snippet=make_snippet(body, qt))
            for s, doc, body in scored[: max(1, limit)]
        ]

    def read(self, doc_id: str, max_chars: int = 20_000) -> str:
        for doc, body in self._all():
            if doc.doc_id == doc_id:
                if len(body) > max_chars:
                    return body[:max_chars] + f"\n\n…（已截断，原文 {len(body)} 字符）"
                return body
        known = ", ".join(sorted(d.doc_id for d, _ in self._all())[:20])
        raise KnowledgeError(
            f"文档 {doc_id!r} 不存在。可用（前 20 个）：{known or '（知识库为空）'}"
        )

    def list_docs(self, prefix: str = "", limit: int = 50) -> list[KnowledgeDoc]:
        docs = [d for d, _ in self._all()]
        if prefix:
            docs = [d for d in docs if d.doc_id.startswith(prefix)]
        docs.sort(key=lambda d: d.doc_id)
        return docs[: max(1, limit)]

    def describe(self) -> str:
        if not self._roots:
            return (
                "知识库：LocalDocsBackend —— **没有找到任何文档目录**\n"
                f"  已查找：<工作目录>/{_PROJECT_DIR}、{_USER_DIR}\n"
                "  建一个目录并放几篇 markdown 进去就能用。"
            )
        n = len(self._all())
        lines = [f"知识库：LocalDocsBackend（本地文档，共 {n} 篇）"]
        for root, source in self._roots:
            cnt = sum(1 for _ in root.rglob("*") if _.is_file() and _.suffix.lower() in TEXT_SUFFIXES)
            lines.append(f"  {source:<8} {root}  （{cnt} 篇）")
        return "\n".join(lines)


def _fmt_time(ts: float) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


__all__ = ["LocalDocsBackend", "MAX_DOCS", "MAX_DOC_BYTES", "TEXT_SUFFIXES"]

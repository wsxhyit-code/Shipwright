# 知识库工具：让 agent 能查内部规范 / SOP / 架构说明。
#
# 设计对齐 `tools/ops/tools.py`：同样是「工具类只依赖抽象后端」，
# 接真实系统时只换后端实现，工具代码一行不改。
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.knowledge.backend import (
    KnowledgeBackend,
    KnowledgeError,
    require_backend,
)


class _KnowledgeTool(Tool):
    """统一把业务异常转成可读的 ToolResult，让模型自己调整。"""

    def __init__(self, backend: KnowledgeBackend | None) -> None:
        self._backend = backend

    def _require(self) -> KnowledgeBackend:
        return require_backend(self._backend)

    async def execute(self, params: BaseModel) -> ToolResult:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SearchKnowledge
# ---------------------------------------------------------------------------


class SearchKnowledgeParams(BaseModel):
    query: str = Field(description="要查什么。用自然语言描述，例如「错误处理和日志的规范」")
    limit: int = Field(default=5, description="最多返回几条（默认 5）")


class SearchKnowledgeTool(_KnowledgeTool):
    name = "SearchKnowledge"
    description = (
        "检索企业内部知识库：编码规范、数据库变更规范、架构约定、线上变更 SOP、排障手册。\n"
        "**动手改代码之前先查一次** —— 内部规范经常和通用最佳实践不一样，"
        "照着通用习惯写出来的代码可能直接违反内部约定。\n"
        "返回的是**命中的片段**（不是全文）。要看完整内容再用 ReadKnowledge。\n"
        "如果没查到，换几个关键词再试一次；仍然没有时请把它当成"
        "「知识库里没有」，而不是「没有相关规定」—— 前者是事实，后者是猜测。"
    )
    params_model = SearchKnowledgeParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: SearchKnowledgeParams = params  # type: ignore[assignment]
        try:
            backend = self._require()
            hits = backend.search(p.query, p.limit)
        except KnowledgeError as e:
            return ToolResult(output=str(e), is_error=True)

        if not hits:
            return ToolResult(
                output=(
                    f"知识库里没有匹配「{p.query}」的内容。\n\n"
                    "这表示**知识库中没有**相关条目，不表示「没有相关规定」。\n"
                    "建议：换同义词再搜一次（例如「错误码」↔「异常处理」）；"
                    "或用 ListKnowledgeDocs 看看知识库里到底有哪些文档。"
                )
            )

        lines = [f"知识库检索「{p.query}」命中 {len(hits)} 条："]
        for h in hits:
            lines.append(
                f"\n  [{h.doc.doc_id}] {h.doc.title}   (相关度 {h.score})"
                f"\n      来源：{h.doc.source}"
                + (f"  {h.doc.location}" if h.doc.location else "")
                + (f"\n      标签：{', '.join(h.doc.tags)}" if h.doc.tags else "")
                + f"\n      {h.snippet}"
            )
        lines.append("\n需要完整内容就用 ReadKnowledge(doc_id=...)。")
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# ReadKnowledge
# ---------------------------------------------------------------------------


class ReadKnowledgeParams(BaseModel):
    doc_id: str = Field(description="文档 ID，从 SearchKnowledge 或 ListKnowledgeDocs 拿到")
    max_chars: int = Field(default=20_000, description="最多读多少字符（防止撑爆上下文）")


class ReadKnowledgeTool(_KnowledgeTool):
    name = "ReadKnowledge"
    description = (
        "读取知识库里某篇文档的完整内容。\n"
        "先用 SearchKnowledge 拿到 `doc_id`，再读具体的某一条。\n"
        "内容超过上限时会被截断，并明确告诉你截断位置 —— "
        "看到截断标记时不要根据残缺内容下结论。"
    )
    params_model = ReadKnowledgeParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ReadKnowledgeParams = params  # type: ignore[assignment]
        if p.max_chars <= 0:
            return ToolResult(output="max_chars 必须为正数", is_error=True)
        try:
            backend = self._require()
            body = backend.read(p.doc_id, min(p.max_chars, 200_000))
        except KnowledgeError as e:
            return ToolResult(output=str(e), is_error=True)
        return ToolResult(output=f"【{p.doc_id}】\n\n{body}")


# ---------------------------------------------------------------------------
# ListKnowledgeDocs
# ---------------------------------------------------------------------------


class ListKnowledgeDocsParams(BaseModel):
    prefix: str = Field(default="", description="只列 ID 以这个前缀开头的（例如 STD-）")
    limit: int = Field(default=50, description="最多列几条")


class ListKnowledgeDocsTool(_KnowledgeTool):
    name = "ListKnowledgeDocs"
    description = (
        "列出知识库里有哪些文档（只有标题和 ID，没有正文）。\n"
        "不知道内部有什么规范时用它探路，比反复盲搜有效。"
    )
    params_model = ListKnowledgeDocsParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ListKnowledgeDocsParams = params  # type: ignore[assignment]
        try:
            backend = self._require()
            docs = backend.list_docs(p.prefix, p.limit)
        except KnowledgeError as e:
            return ToolResult(output=str(e), is_error=True)

        if not docs:
            scope = f"（前缀 {p.prefix}）" if p.prefix else ""
            return ToolResult(output=f"知识库里没有文档{scope}。")

        lines = [f"知识库共列出 {len(docs)} 篇："]
        for d in docs:
            lines.append(
                f"  [{d.doc_id}] {d.title}"
                + (f"  · {', '.join(d.tags)}" if d.tags else "")
                + (f"  ({d.source})" if d.source else "")
            )
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


KNOWLEDGE_TOOLS: tuple[type[_KnowledgeTool], ...] = (
    SearchKnowledgeTool,
    ReadKnowledgeTool,
    ListKnowledgeDocsTool,
)


def register_knowledge_tools(
    registry: Any, backend: KnowledgeBackend | None
) -> list[str]:
    for cls in KNOWLEDGE_TOOLS:
        registry.register(cls(backend))
    return [cls.name for cls in KNOWLEDGE_TOOLS]


__all__ = [
    "KNOWLEDGE_TOOLS",
    "ListKnowledgeDocsParams",
    "ListKnowledgeDocsTool",
    "ReadKnowledgeParams",
    "ReadKnowledgeTool",
    "SearchKnowledgeParams",
    "SearchKnowledgeTool",
    "register_knowledge_tools",
]

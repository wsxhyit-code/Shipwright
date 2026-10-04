"""按配置构造知识库后端。

    toolset:
      knowledge:
        kind: local                  # 开箱可用：扫本地 markdown
        extra_dirs: ["docs/standards"]
        include_user_dir: true

        # 或者接内部检索 API：
        # kind: http
        # base_url: "https://kb.internal/api"
        # path: "/search"
        # token_env: "KB_TOKEN"
        # read_path: "/docs/{id}"

只支持**一个**知识库源（不像 ops 那样按能力拆多个）。
理由：检索是"一个入口、返回一批结果"的语义，多个来源应该在
检索服务那一层做联邦，而不是让 agent 面对三个 SearchKnowledge。
需要覆盖多个本地目录时用 `extra_dirs`。
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from mewcode.tools.knowledge.backend import (
    KnowledgeBackend,
    KnowledgeError,
    MockKnowledgeBackend,
)
from mewcode.tools.knowledge.backends.http_api import HttpKnowledgeBackend
from mewcode.tools.knowledge.backends.local_docs import LocalDocsBackend

KINDS = ("mock", "local", "http")


def _resolve_token(spec: dict, env: Mapping[str, str], label: str) -> str:
    token_env = (spec.get("token_env") or "").strip()
    if not token_env:
        return (spec.get("token") or "").strip()
    value = (env.get(token_env) or "").strip()
    if not value:
        raise KnowledgeError(
            f"{label}: 环境变量 {token_env} 没有设置或为空。\n"
            f"请在启动 agent 前导出它，例如：export {token_env}=<你的只读 token>\n"
            f"（如果这个知识库允许匿名访问，请把 token_env 那一行删掉，"
            f"而不是留一个读不到的环境变量名。）"
        )
    return value


def build_knowledge_backend(
    spec: dict | None,
    work_dir: str,
    env: Mapping[str, str] | None = None,
) -> KnowledgeBackend | None:
    """把配置里的一段 knowledge 拼成后端。没配就返回 None。

    返回 None 时工具仍然会注册，但调用会抛 `KnowledgeUnavailable` ——
    这样模型能明确知道「没接入」，而不是把空结果读成「内部没有规定」。
    """
    if not spec:
        return None

    kind = spec.get("kind")
    label = f"toolset.knowledge (kind={kind})"
    env = os.environ if env is None else env

    if kind == "mock":
        return MockKnowledgeBackend()

    if kind == "local":
        return LocalDocsBackend(
            work_dir,
            extra_dirs=list(spec.get("extra_dirs") or []),
            include_user_dir=bool(spec.get("include_user_dir", True)),
        )

    if kind == "http":
        if not spec.get("base_url"):
            raise KnowledgeError(f"{label}: kind=http 需要 base_url")
        return HttpKnowledgeBackend(
            spec["base_url"],
            path=spec.get("path") or "/search",
            query_param=spec.get("query_param") or "q",
            limit_param=spec.get("limit_param") or "limit",
            read_path=spec.get("read_path") or "",
            read_id_field=spec.get("read_id_field") or "content",
            token=_resolve_token(spec, env, label),
            timeout=float(spec.get("timeout", 15.0)),
            trust_env=bool(spec.get("trust_env", False)),
            mapping=spec.get("mapping") or None,
            name=spec.get("name") or "内部知识库",
        )

    raise KnowledgeError(
        f"{label}: 不认识的知识库类型 {kind!r}，可选：{', '.join(KINDS)}"
    )


def knowledge_report(backend: KnowledgeBackend | None) -> str:
    """给模型看的可用性说明。

    没配置时**也要出内容** —— 否则模型不知道自己没有知识库，
    会凭通用习惯写代码，还以为符合内部标准。
    """
    if backend is None:
        return (
            "知识库：**未接入**\n"
            "  调用 SearchKnowledge / ReadKnowledge 会直接报错。\n"
            "  这不代表「内部规范里没有相关规定」，只是没有接入数据源。\n"
            "  在没接入的情况下，不要假设自己的写法和内部标准一致。"
        )
    describe = getattr(backend, "describe", None)
    return describe() if callable(describe) else ""


__all__ = ["KINDS", "build_knowledge_backend", "knowledge_report"]

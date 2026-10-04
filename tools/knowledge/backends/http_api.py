"""HTTP 后端：接企业内部的检索 / Wiki / 文档 API。

## 为什么做成「可映射」的

各家的响应形状不一样（ES 是 `hits.hits`、Confluence 是 `results`、
自研检索网关又是另一套）。如果写死一种形状，这个后端对大多数人就没用。

所以这里把「从响应里哪里取数组、每个字段叫什么」做成配置项，
默认值按最常见的形状：

    {"results": [
        {"id": "...", "title": "...", "snippet": "...", "url": "...",
         "tags": ["..."], "updated": "2026-09-28"},
        ...
    ]}

接自己的系统时只改 `response_path` / `*_field` 这几个配置，不用改代码。

## 和其他后端一样的两条原则

**① `trust_env=False` 默认关掉。**
httpx 在 Windows 上会从**注册表**读系统代理（不是环境变量），
内网地址被塞进代理会拿到一个没有任何线索的 502。这个坑在
`tools/ops/backends/http_backends.py` 里踩过一次，这里沿用同样的处理。

**② 出错就抛，绝不返回空列表。**
HTTP 500 / 非 JSON / 结构不对 → 抛 `KnowledgeError`。
只有「请求成功且结果确实是空数组」才算「没查到」。
两者混淆会让 agent 把「检索服务挂了」读成「内部没有这条规范」。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from mewcode.tools.knowledge.backend import (
    KnowledgeDoc,
    KnowledgeError,
    KnowledgeHit,
)

DEFAULT_MAPPING = {
    "response_path": "results",   # 点号路径，从响应里找到结果数组
    "id_field": "id",
    "title_field": "title",
    "snippet_field": "snippet",
    "url_field": "url",
    "tags_field": "tags",
    "updated_field": "updated",
}


class HttpKnowledgeBackend:
    """把内部检索 API 包装成 `KnowledgeBackend`。"""

    def __init__(
        self,
        base_url: str,
        *,
        path: str = "/search",
        query_param: str = "q",
        limit_param: str = "limit",
        read_path: str = "",
        read_id_field: str = "content",
        token: str = "",
        timeout: float = 15.0,
        trust_env: bool = False,
        client: httpx.Client | None = None,
        mapping: dict[str, str] | None = None,
        name: str = "内部知识库",
    ) -> None:
        if not base_url:
            raise KnowledgeError("HttpKnowledgeBackend 需要 base_url")
        self.base_url = base_url.rstrip("/")
        self._path = path
        self._query_param = query_param
        self._limit_param = limit_param
        self._read_path = read_path
        self._read_id_field = read_id_field
        self._timeout = timeout
        self._name = name
        self._map = {**DEFAULT_MAPPING, **(mapping or {})}

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            trust_env=trust_env,   # 见模块 docstring 的 ①
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- HTTP ---

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise KnowledgeError(f"{self._name} 请求失败：{e}") from e
        if resp.status_code >= 400:
            raise KnowledgeError(
                f"{self._name} 返回 {resp.status_code}：{resp.text[:200]}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise KnowledgeError(
                f"{self._name} 返回的不是 JSON：{resp.text[:200]}"
            ) from e

    def _results(self, payload: Any) -> list[dict[str, Any]]:
        node = payload
        for part in self._map["response_path"].split("."):
            if not part:
                continue
            if not isinstance(node, dict) or part not in node:
                raise KnowledgeError(
                    f"{self._name} 的响应里找不到 `{self._map['response_path']}`。\n"
                    f"收到的顶层键：{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}\n"
                    f"如果你的 API 形状不同，改配置里的 toolset.knowledge.mapping.response_path。"
                )
            node = node[part]
        if node is None:
            return []
        if not isinstance(node, list):
            raise KnowledgeError(
                f"{self._name} 的 `{self._map['response_path']}` 不是数组，"
                f"而是 {type(node).__name__}"
            )
        return [x for x in node if isinstance(x, dict)]

    def _to_hit(self, item: dict[str, Any]) -> KnowledgeHit | None:
        doc_id = item.get(self._map["id_field"])
        if doc_id in (None, ""):
            return None
        title = item.get(self._map["title_field"]) or str(doc_id)
        raw_tags = item.get(self._map["tags_field"]) or []
        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.replace("，", ",").split(",") if t.strip()]
        if not isinstance(raw_tags, list):
            raw_tags = []
        doc = KnowledgeDoc(
            doc_id=str(doc_id),
            title=str(title),
            source="http",
            location=str(item.get(self._map["url_field"]) or ""),
            tags=[str(t) for t in raw_tags],
            updated=str(item.get(self._map["updated_field"]) or ""),
        )
        snippet = str(item.get(self._map["snippet_field"]) or "")
        # 有些检索网关用 score 字段；没有就按 1.0 表示"是服务端排好序的"
        try:
            score = float(item.get("score", 1.0))
        except (TypeError, ValueError):
            score = 1.0
        return KnowledgeHit(doc=doc, score=round(score, 4), snippet=snippet[:800])

    # --- KnowledgeBackend ---

    def search(self, query: str, limit: int = 5) -> list[KnowledgeHit]:
        payload = self._get(
            self._path, {self._query_param: query, self._limit_param: max(1, limit)}
        )
        hits = [h for h in (self._to_hit(x) for x in self._results(payload)) if h]
        return hits[: max(1, limit)]

    def read(self, doc_id: str, max_chars: int = 20_000) -> str:
        if not self._read_path:
            raise KnowledgeError(
                f"{self._name} 只配了检索、没有配「按 ID 取正文」的接口，"
                "所以读不了完整文档。\n"
                "解决办法二选一：\n"
                "  · 用上面检索结果里的 snippet 判断即可，不读全文；\n"
                "  · 或配置 toolset.knowledge.read_path（例如 /docs/{id}），"
                "并把 read_id_field 指向正文所在的字段。"
            )
        path = self._read_path.replace("{id}", doc_id)
        payload = self._get(path, {})
        node: Any = payload
        for part in self._read_id_field.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if node is None:
            raise KnowledgeError(
                f"{self._name} 的响应里找不到正文（read_id_field="
                f"{self._read_id_field!r}）。收到的顶层键："
                f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
            )
        body = node if isinstance(node, str) else json.dumps(node, ensure_ascii=False, indent=2)
        if len(body) > max_chars:
            return body[:max_chars] + f"\n\n…（已截断，原文 {len(body)} 字符）"
        return body

    def list_docs(self, prefix: str = "", limit: int = 50) -> list[KnowledgeDoc]:
        # 通用检索 API 通常没有"列全部"的接口，所以用空查询探一次；
        # 探不到就明确说清楚，而不是假装知识库是空的。
        try:
            payload = self._get(self._path, {self._query_param: "", self._limit_param: max(1, limit)})
            items = self._results(payload)
        except KnowledgeError:
            raise KnowledgeError(
                f"{self._name} 没有提供「列出全部文档」的能力。\n"
                "请改用 SearchKnowledge 按问题检索 —— 这也是更省上下文的方式。"
            )
        docs = []
        for x in items:
            h = self._to_hit(x)
            if h and (not prefix or h.doc.doc_id.startswith(prefix)):
                docs.append(h.doc)
            if len(docs) >= max(1, limit):
                break
        return docs

    def describe(self) -> str:
        lines = [f"知识库：HttpKnowledgeBackend —— {self._name}", f"  地址：{self.base_url}{self._path}"]
        lines.append(f"  取正文：{'支持（' + self._read_path + '）' if self._read_path else '不支持（只检索）'}")
        lines.append(f"  字段映射：{self._map}")
        return "\n".join(lines)


__all__ = ["DEFAULT_MAPPING", "HttpKnowledgeBackend"]

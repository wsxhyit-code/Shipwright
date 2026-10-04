"""企业知识库抽象：让 agent 能查「内部规范 / SOP / 架构说明 / 排障手册」。

工具层只依赖下面这几个方法。接真实系统时实现 `KnowledgeBackend`
（内部检索 API、Confluence、Wiki、代码规范库都能对上一个方法），
**工具代码一行都不用改**。

## 为什么这是「方向二」最缺的一块

方向二要的是「接入企业内部的知识库、代码规范、私有工具链，让 agent
写出来的代码天然符合内部标准」。而此前项目里**没有任何检索能力**：
0 个 embedding、0 个向量库、0 个 RAG。

最接近的只有 `MEWCODE.md`（静态注入一段规范）和 `memory/`（会话记忆）。
静态注入的问题是：规范一多就撑爆 prompt，而且 agent 不知道自己缺什么。

所以这里做成**两层**：

    SearchKnowledge → 按问题检索，只回「命中的片段」
    ReadKnowledge   → 针对某一条，取回完整内容（有上限）

## ⚠️ 三条刻意的设计

**① 不做向量检索，做关键词检索。**

本地文档后端用**关键词打分**（BM25 风格），不引入 embedding 模型。
理由：引入向量库要多一个几十 MB 的模型 + 一个向量库依赖，而「内部规范」
这种场景下关键词完全够用；真需要语义检索时，正确做法是**把内部检索
API 接在后面**（`HttpKnowledgeBackend`），而不是在 agent 进程里塞一个模型。

**② 中文必须按字符切，不能按空格。**

按空格切词对中文完全失效 —— 一整段中文会变成一个 token，永远匹配不上。
所以分词规则是：ASCII 按单词（小写化），CJK 按**字符二元组**。
这是中文知识库能不能用的分水岭。

**③ 「没接入」和「没查到」必须可区分。**

没接后端 → 抛 `KnowledgeUnavailable`（明确报错）。
接了但确实没有 → 返回空列表。
两者混淆的后果和运维工具那边一样：agent 会把「查不到」读成「不存在」，
然后基于缺失的信息给出一个很自信的错误结论。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# 领域模型
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeDoc:
    """知识库里的一篇文档（只含元信息，不含正文）。"""

    doc_id: str
    title: str
    source: str = ""          # 来自哪（project / user / http / builtin）
    location: str = ""        # 文件路径或 URL
    tags: list[str] = field(default_factory=list)
    updated: str = ""         # 人类可读的时间，例如 "2026-09-28"


@dataclass
class KnowledgeHit:
    """一条检索结果。`snippet` 是命中的片段，**不是全文**。"""

    doc: KnowledgeDoc
    score: float
    snippet: str = ""


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class KnowledgeError(Exception):
    """知识库业务错误。工具层会转成 is_error=True 的可读结果。"""


class KnowledgeUnavailable(KnowledgeError):
    """知识库没接入。

    刻意**抛错而不是返回空列表** —— 返回空会让 agent 把「没接入」读成
    「内部规范里没有这条」，然后凭自己的习惯写代码，还以为符合标准。
    """


# ---------------------------------------------------------------------------
# 后端接口
# ---------------------------------------------------------------------------


class KnowledgeBackend(Protocol):
    """工具层只依赖这些方法。全部只读。"""

    def search(self, query: str, limit: int = 5) -> list[KnowledgeHit]: ...
    def read(self, doc_id: str, max_chars: int = 20_000) -> str: ...
    def list_docs(self, prefix: str = "", limit: int = 50) -> list[KnowledgeDoc]: ...
    def describe(self) -> str: ...


# ---------------------------------------------------------------------------
# 分词与打分（本地文档后端要用，单独放这里方便测）
# ---------------------------------------------------------------------------

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_STOP = frozenset("的了和是在有与及或对把被这那为以于")


def tokenize(text: str) -> list[str]:
    """把文本切成可比较的 token。

    规则（**中文必须走字符二元组**，否则整段中文会变成一个 token）：
      · ASCII：按单词切，小写化，长度 ≥ 2
      · CJK  ：滑动取**二元组**（"代码规范" -> 代码/码规/规范）

    二元组是中文检索里最省事且够用的做法：不需要分词词典，
    也不会因为分词粒度不同而漏召回。
    """
    s = text.lower()
    tokens: list[str] = []

    for w in _ASCII_WORD_RE.findall(s):
        if len(w) >= 2:
            tokens.append(w)

    for run in _CJK_RE.findall(s):
        if len(run) == 1:
            if run not in _CJK_STOP:
                tokens.append(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram[0] in _CJK_STOP and bigram[1] in _CJK_STOP:
                continue
            tokens.append(bigram)

    return tokens


def build_idf(token_lists: list[list[str]]) -> dict[str, float]:
    """按文档频率算 IDF 权重。

    ## 为什么需要它

    二元组匹配有个天性：`存在`、`的话`、`问题` 这类常见词在中文文档里
    到处都是，于是**一个毫无意义的查询也会命中一堆文档**。实测：
    用「zzz不存在的话题」去搜，能返回 2 条看起来很像样的结果 ——
    而 agent 很可能拿其中一条当真。

    所以给每个 token 按「出现在多少篇文档里」加权：

        出现在所有文档里的词（df == N）→ 权重 ≈ 0，等于不参与
        只出现在一篇文档里的词（df == 1）→ 权重最高

    公式用平滑过的 `log((N + 1) / (df + 0.5))`，避免除零和负权重。
    """
    n = max(1, len(token_lists))
    df: dict[str, int] = {}
    for toks in token_lists:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    import math

    return {t: math.log((n + 1) / (d + 0.5)) for t, d in df.items()}


#: 低于这个相关度的结果会被丢掉。
#:
#: ## 这个数是怎么来的（实测，不是拍的）
#:
#: 在本项目自己的 4 篇文档语料上，把所有「查询 x 文档」的分数排出来：
#:
#:     0.2388  ← 分界线上方最高的**假命中**
#:              「完全无关的问题」x README：仅有 `完全`+`全无`
#:              共享了一个偶然的三字串「完全无」，实际是跨词巧合
#:     ------ 门槛落在这里 ------
#:     0.2862  ← 分界线下方最低的**真命中**
#:              「zzz不存在的话题」x mcp-ops：文档里真的有「不存在」这个连续词
#:     0.3262  「日志聚类怎么做」x SKILL：文档里真的有「日志聚类」
#:
#: 所以门槛取两者之间。**但要诚实说明它的局限**：
#: 这个间隔只有 ~20%，而且分数的绝对尺度取决于语料大小和文档长度
#: （归一化项是 `1 + len(body_tokens)/500`）。换一批文档，这个数就不准了。
#:
#: 真正承重的不是这个数，而是前面三道**结构性**的判定：
#: 连续佐证（幻影 token）、IDF（到处都出现的词）、覆盖度（长查询只中一个词）。
#: 它们不依赖分数尺度。这个数只是最后一道粗筛。
#: 接真实检索系统时（HttpKnowledgeBackend）应当改用对方返回的相关度。
MIN_SCORE = 0.26


def min_matches_for(query_tokens: list[str]) -> int:
    """这条查询至少要命中几个**不同的** token 才算数。

    ## 为什么需要「覆盖度」而不只是分数

    这是**第二道**防线，和 IDF、连续佐证是三个独立的角度：

        IDF       —— 到处都出现的词降权（"存在""的话"）
        连续佐证  —— 跨词巧合的幻影 token 降权（"全无"来自"完全无法"）
        覆盖度    —— 长查询只命中一个词算弱匹配（这一条）

    长查询只命中一个词、却靠那个词的高 IDF 过线，是典型的假命中：

        查询只有 1~2 个词 → 命中 1 个就算数（否则短查询永远搜不到）
        查询有 3 个词以上 → 至少要命中 2 个不同的词

    ## 说清楚这条防线**没**做到什么

    加这条的时候以为它能挡住「完全无关的问题」，**结果并没有** ——
    那篇 README 命中了 5 个 token（完全/全无/关的/无关/问题），
    覆盖度绰绰有余，真正的问题是其中 `全无`、`关的` 根本不是一个词。
    所以挡下它的是**连续佐证**，不是覆盖度。

    留着覆盖度是因为它挡另一种情况：只有一个词命中、却因为
    IDF 高而单独过线。别指望它挡其它东西。
    """
    return 1 if len(set(query_tokens)) <= 2 else 2


def corroborated_tokens(query: str, doc_text: str) -> set[str]:
    """挑出查询里**拿到了连续证据**的 token。

    ## 为什么必须有这一步（中文二元组分词的固有坑）

    二元组会把**词边界**也切成 token。于是同一串字符在两边都被切出
    一个实际上不存在的「词」：

        「完全无法区分」   -> 完全 / **全无** / 无法 / ...
        「完全无关的问题」 -> 完全 / **全无** / 无关 / ...

    两边的 `全无` 都不是词，纯属跨词巧合。它在这类语料里杀伤力极大：
    `全无` 只在一篇文档里出现，IDF 拿到最高的 1.20，
    于是「完全无关的问题」靠着 `完全` + `全无` 两个幻影 token
    把一篇和它毫不相干的 README 顶到了 0.554 分。

    同类还有 `关的`（来自「相**关的**断言」），IDF 同样是 1.20。

    ## 判据

    一个 token 只有在「能和查询里**相邻**的 token 拼成 >=3 个连续字符」
    并且「这 >=3 个字符在文档里也连续出现」时，才算独立证据：

        `的三条` + `三条铁` + `条铁律`  -> 都落在「三条铁律」里 -> 有佐证
        `全无`                          -> 只有「完全无」是巧合     -> 无佐证

    **短查询例外**：整段 CJK <= 3 字时，它本身就是完整短语，不需要佐证。
    否则「沙箱」「回滚」这种独立词永远搜不到 —— 那才是真的坏了。

    ASCII token 是整词，不存在跨词边界问题，一律算有佐证。
    """
    low = query.lower()
    doc = doc_text.lower()
    out: set[str] = set()

    for w in _ASCII_WORD_RE.findall(low):
        if len(w) >= 2:
            out.add(w)

    for run in _CJK_RE.findall(low):
        if len(run) <= 3:
            out.update(run)
            out.update(run[i : i + 2] for i in range(len(run) - 1))
            continue
        for i in range(len(run) - 1):
            right = run[i : i + 3]
            left = run[i - 1 : i + 2] if i > 0 else ""
            if (len(right) == 3 and right in doc) or (len(left) == 3 and left in doc):
                out.add(run[i : i + 2])

    return out


#: 只有跨词巧合证据的 token，权重乘以这个系数（而不是直接丢掉）。
#:
#: 丢掉太狠：`沙箱的三条铁律` 里 `沙箱` 是独立词、拿不到连续佐证，
#: 直接丢会把查询里最关键的词扔掉。降权则留着它、但不让它主导排序。
CORROBORATION_DISCOUNT = 0.25


@dataclass(frozen=True)
class ScoreBreakdown:
    """一次打分的完整结果。工具层要靠它说清楚「为什么命中」。"""

    score: float
    matched: tuple[str, ...] = ()      # 命中的不同 token（字典序）
    discounted: tuple[str, ...] = ()   # 其中只靠跨词巧合命中的

    @property
    def n_matched(self) -> int:
        return len(self.matched)


def score_and_match(
    query: str,
    *,
    title: str,
    tags: list[str],
    body: str,
    idf: dict[str, float] | None = None,
) -> ScoreBreakdown:
    """给一篇文档打分，并说清楚命中了哪些 token。

    ## 为什么入参是**查询原文**而不是 token 列表

    因为「这个命中是真词还是跨词巧合」只有拿到查询原文才判得出来
    （见 `corroborated_tokens`）。让调用方自己切好 token 再传进来，
    就等于把这条防护交给调用方去记得 —— 迟早会漏。

    权重：标题 5 > 标签 3 > 正文 1，再乘该词的 IDF。
    """
    qt = tokenize(query)
    if not qt:
        return ScoreBreakdown(0.0)
    qset = set(qt)

    title_tokens = set(tokenize(title))
    tag_tokens = set(tokenize(" ".join(tags)))
    body_tokens = set(tokenize(body))

    matched = qset & (title_tokens | tag_tokens | body_tokens)
    if not matched:
        return ScoreBreakdown(0.0)

    ok = corroborated_tokens(query, f"{title} {' '.join(tags)} {body}")

    def w(t: str) -> float:
        base = idf.get(t, 1.0) if idf else 1.0
        return base if t in ok else base * CORROBORATION_DISCOUNT

    hit = 5.0 * sum(w(t) for t in qset & title_tokens)
    hit += 3.0 * sum(w(t) for t in qset & tag_tokens)
    hit += 1.0 * sum(w(t) for t in qset & body_tokens)

    # 归一化：避免长文档仅因为字多就排前面
    score = hit / (1.0 + len(body_tokens) / 500.0)
    return ScoreBreakdown(
        score=round(score, 6),
        matched=tuple(sorted(matched)),
        discounted=tuple(sorted(matched - ok)),
    )


def score_doc(
    query: str,
    *,
    title: str,
    tags: list[str],
    body: str,
    idf: dict[str, float] | None = None,
) -> float:
    """只要分数（保留这个入口是为了易读）。"""
    return score_and_match(query, title=title, tags=tags, body=body, idf=idf).score


def make_snippet(body: str, query_tokens: list[str], width: int = 220) -> str:
    """从正文里裁出**命中附近**的一段，而不是从头截。

    从头截的话，命中的关键词可能在文档末尾，模型看到片段也判断不出相关性。

    ## 实现说明（踩过的坑）

    最早写的是「按步长滑动窗口、每格跑一次 tokenize 数命中数」。它有两个问题：

      · **有个致命的提前退出**：扫过 400 字符还没命中就放弃 ——
        于是命中在文档末尾时永远找不到，正好是这段代码要防的情况。
      · 每格都 tokenize 一次，长文档上很慢。

    现在改成**直接用 token 做子串定位**：找最早出现的那个 token 位置，
    把窗口中心放上去（前面留 1/3 做上下文）。O(n) 一次扫描，而且准确。
    """
    if not body:
        return ""
    low = body.lower()
    hit_at = -1
    for t in sorted(set(query_tokens), key=len, reverse=True):
        i = low.find(t)
        if i >= 0 and (hit_at < 0 or i < hit_at):
            hit_at = i

    if hit_at < 0:
        start = 0
    else:
        start = max(0, hit_at - width // 3)

    snippet = body[start : start + width].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + width < len(body) else ""
    return f"{prefix}{snippet}{suffix}"


# ---------------------------------------------------------------------------
# 内存实现（给测试与演示用）
# ---------------------------------------------------------------------------


class MockKnowledgeBackend:
    """内存知识库，内含一份「某公司的内部规范」示例。

    用途：让 `SearchKnowledge` / `ReadKnowledge` 这两条链路能在
    **不接任何外部系统**的情况下跑通和被测。
    """

    def __init__(self, docs: dict[str, tuple[str, list[str], str]] | None = None) -> None:
        # doc_id -> (title, tags, body)
        self._docs: dict[str, tuple[str, list[str], str]] = docs or {
            "STD-001": (
                "错误处理规范",
                ["规范", "异常", "错误"],
                "所有对外接口必须返回结构化错误码，禁止把异常直接抛给调用方。\n"
                "日志里禁止打印用户隐私字段（手机号、身份证、token）。\n"
                "错误码统一在 errors.py 里定义，不允许在业务代码里硬编码字符串。",
            ),
            "STD-002": (
                "数据库变更规范",
                ["规范", "数据库", "迁移"],
                "任何 schema 变更都必须提供可回滚的迁移脚本，并经过一次演练。\n"
                "禁止在业务代码里写裸 SQL，统一走 repository 层。\n"
                "给大表加索引必须用 CONCURRENTLY，避免长时间锁表。",
            ),
            "ARCH-001": (
                "服务分层架构",
                ["架构", "分层"],
                "controller 层只做参数校验与响应组装；\n"
                "service 层承载业务逻辑，禁止直接访问数据库；\n"
                "repository 层独占数据访问。跨层调用一律禁止。",
            ),
            "RUN-001": (
                "线上变更 SOP",
                ["SOP", "发布", "回滚"],
                "发布前必须确认有回滚方案；灰度先从 1 个实例开始，观察 10 分钟。\n"
                "回滚优先级高于定位根因 —— 先止血再复盘。\n"
                "任何变更都要在变更群里同步。",
            ),
        }

    def search(self, query: str, limit: int = 5) -> list[KnowledgeHit]:
        qt = tokenize(query)
        corpus = [tokenize(f"{t} {' '.join(tags)} {body}")
                  for t, tags, body in self._docs.values()]
        idf = build_idf(corpus)

        scored: list[tuple[float, str]] = []
        need = min_matches_for(qt)
        for doc_id, (title, tags, body) in self._docs.items():
            sb = score_and_match(query, title=title, tags=tags, body=body, idf=idf)
            if sb.score >= MIN_SCORE and sb.n_matched >= need:
                scored.append((sb.score, doc_id))
        scored.sort(key=lambda x: (-x[0], x[1]))

        out: list[KnowledgeHit] = []
        for s, doc_id in scored[: max(1, limit)]:
            title, tags, body = self._docs[doc_id]
            out.append(
                KnowledgeHit(
                    doc=self._doc(doc_id, title, tags),
                    score=round(s, 4),
                    snippet=make_snippet(body, qt),
                )
            )
        return out

    def read(self, doc_id: str, max_chars: int = 20_000) -> str:
        if doc_id not in self._docs:
            known = ", ".join(sorted(self._docs))
            raise KnowledgeError(f"文档 {doc_id} 不存在。可用的有：{known}")
        _title, _tags, body = self._docs[doc_id]
        if len(body) > max_chars:
            return body[:max_chars] + f"\n\n…（已截断，原文 {len(body)} 字符）"
        return body

    def list_docs(self, prefix: str = "", limit: int = 50) -> list[KnowledgeDoc]:
        out = []
        for doc_id in sorted(self._docs):
            if prefix and not doc_id.startswith(prefix):
                continue
            title, tags, _ = self._docs[doc_id]
            out.append(self._doc(doc_id, title, tags))
            if len(out) >= limit:
                break
        return out

    def describe(self) -> str:
        return (
            f"知识库：MockKnowledgeBackend（演示用，{len(self._docs)} 篇内置文档）\n"
            f"  文档：{', '.join(sorted(self._docs))}\n"
            "  注意：这是内存里的演示数据，不连任何真实系统。"
        )

    def _doc(self, doc_id: str, title: str, tags: list[str]) -> KnowledgeDoc:
        return KnowledgeDoc(
            doc_id=doc_id, title=title, source="mock", location="(内存)", tags=list(tags)
        )


def require_backend(backend: KnowledgeBackend | None) -> KnowledgeBackend:
    """取后端；没有就抛 `KnowledgeUnavailable`（而不是返回空）。"""
    if backend is None:
        raise KnowledgeUnavailable(
            "知识库没有接入，无法检索内部规范 / SOP / 架构说明。\n"
            "注意：这不代表「内部规范里没有相关规定」，只是没有接入数据源。\n"
            "配置方式见 .mewcode/config.yaml.example 的 toolset.knowledge 段。"
        )
    return backend


__all__ = [
    "MIN_SCORE",
    "KnowledgeBackend",
    "KnowledgeDoc",
    "KnowledgeError",
    "KnowledgeHit",
    "KnowledgeUnavailable",
    "MockKnowledgeBackend",
    "build_idf",
    "corroborated_tokens",
    "CORROBORATION_DISCOUNT",
    "make_snippet",
    "min_matches_for",
    "require_backend",
    "score_and_match",
    "score_doc",
    "ScoreBreakdown",
    "tokenize",
]

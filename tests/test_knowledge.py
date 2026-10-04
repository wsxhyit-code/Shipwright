# 企业知识库的测试。
#
# 重点守三件事：
#   ① **中文必须按字符二元组切词** —— 按空格切的话一整段中文会变成一个
#      token，永远匹配不上，中文知识库直接等于没有
#   ② **「没接入」和「没查到」必须可区分** —— 前者抛错，后者返回空
#   ③ 检索结果只回「命中的片段」而不是全文（上下文预算）
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mewcode.config import KnowledgeConfig, ToolsetConfig
from mewcode.toolset import assemble_toolset
from mewcode.tools import create_default_registry
from mewcode.tools.knowledge import (
    KnowledgeError,
    KnowledgeUnavailable,
    MockKnowledgeBackend,
    tokenize,
)
from mewcode.tools.knowledge.backend import (
    MIN_SCORE,
    corroborated_tokens,
    make_snippet,
    min_matches_for,
    require_backend,
    score_and_match,
    score_doc,
)
from mewcode.tools.knowledge.backends.factory import (
    build_knowledge_backend,
    knowledge_report,
)
from mewcode.tools.knowledge.backends.http_api import HttpKnowledgeBackend
from mewcode.tools.knowledge.backends.local_docs import LocalDocsBackend
from mewcode.validator import ConfigError, validate_knowledge, validate_toolset


# ---------------------------------------------------------------------------
# 一、中文分词 —— 这是分水岭
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_chinese_becomes_bigrams(self):
        """★ 中文按**二元组**切。

        如果按空格切，`错误处理规范` 会变成单个 token，
        用户搜 `错误处理` 永远匹配不上 —— 中文知识库就等于没有。
        """
        toks = tokenize("错误处理规范")
        assert "错误" in toks
        assert "误处" in toks
        assert "处理" in toks
        assert "规范" in toks
        # 不能出现整段作为一个 token
        assert "错误处理规范" not in toks

    def test_chinese_phrase_matches_longer_text(self):
        """搜短词能命中含它的长句 —— 这才是能用的检索。"""
        doc_tokens = set(tokenize("所有对外接口必须返回结构化错误码，禁止把异常直接抛给调用方"))
        query_tokens = set(tokenize("错误码"))
        assert doc_tokens & query_tokens, "中文短语搜不到含它的长句"

    def test_ascii_words_lowercased_and_filtered(self):
        toks = tokenize("Use HTTPX Client and a b")
        assert "httpx" in toks
        assert "client" in toks
        assert "use" in toks
        # 单字母被过滤
        assert "a" not in toks
        assert "b" not in toks

    def test_mixed_chinese_and_ascii(self):
        toks = tokenize("禁止 print(token)，用 logger")
        assert "print" in toks
        assert "logger" in toks
        assert "禁止" in toks
        assert "token" in toks

    def test_single_cjk_char_kept_but_stopchars_dropped(self):
        assert "猫" in tokenize("猫")
        assert "的" not in tokenize("的")

    def test_empty_input(self):
        assert tokenize("") == []
        assert tokenize("   \n\t ") == []


# ---------------------------------------------------------------------------
# 二、打分与片段
# ---------------------------------------------------------------------------


class TestScoring:
    def test_title_outweighs_body(self):
        title_hit = score_doc("规范", title="编码规范", tags=[], body="无关内容" * 50)
        body_hit = score_doc("规范", title="无关标题", tags=[], body="规范" * 5)
        assert title_hit > body_hit

    def test_tags_outweigh_body(self):
        tag_hit = score_doc("数据库", title="X", tags=["数据库"], body="无关" * 50)
        body_hit = score_doc("数据库", title="Y", tags=[], body="数据库" * 5)
        assert tag_hit > body_hit

    def test_no_match_scores_zero(self):
        assert score_doc("完全不相干", title="A", tags=[], body="B") == 0.0

    def test_empty_query_scores_zero(self):
        assert score_doc("", title="A", tags=[], body="B") == 0.0

    def test_long_doc_does_not_win_purely_by_length(self):
        """长文档不能仅因为字多就排前面。"""
        short = score_doc("规范", title="规范", tags=[], body="规范")
        long_ = score_doc("规范", title="规范", tags=[], body="规范" + "填充" * 5000)
        assert short > long_


class TestPhantomBigrams:
    """★ 跨词边界的「幻影 bigram」必须被认出来。

    这是中文二元组分词的固有坑，也是实测中**唯一一个真实存在过的假命中来源**：

        文档「完全无法区分」   -> 完全 / 全无 / 无法
        查询「完全无关的问题」 -> 完全 / 全无 / 无关

    两边都有 `全无`，但它不是词，纯属跨词巧合。它只出现在一篇文档里，
    于是 IDF 最高（1.20），把一篇毫不相干的 README 顶到了 0.554 分
    （真实命中一般在 0.4~0.8），过了当时 0.35 的门槛。

    同类的还有 `关的`（来自「相关的断言」），IDF 同样是 1.20。

    ## 为什么这条判定不能只看分数

    试过三条纯几何的替代信号，全部**不能**区分真假命中（实测数据）：

        · 最长连续公共子串：真命中 4 字，假命中 3 字 —— 差别没有意义
        · 命中词的共现窗口：两边都是 3 —— 因为幻影 token 本来就重叠
        · 命中词数 / 查询词数：真 3/5，假 5/6 —— 假的反倒更高

    根本原因是那篇 README 里 `完全`、`无关`、`问题` 都是**真词**，
    只是分散在 8 个地方。所以只能从「这个 token 本身是不是词」下手，
    也就是这里判的「有没有连续佐证」。
    """

    def test_cross_word_bigram_is_not_corroborated(self):
        """`关的` / `无关` 都拿不到连续佐证。"""
        ok = corroborated_tokens(
            "完全无关的问题", "把类型检查报错当无关忽略掉，送相关PR的断言"
        )
        assert "关的" not in ok, "`关的` 是「相关的断言」切出来的幻影，不该算证据"
        assert "无关" not in ok, "文档里没有「无关的」这个连续串"

    def test_contiguous_term_is_corroborated(self):
        """反向对照：真正的连续词必须拿到佐证。

        少了这条对照，一个「永远返回空集」的实现也能让上面那条通过。
        """
        ok = corroborated_tokens("日志聚类怎么做", "本文讲日志聚类的实现")
        assert {"日志", "志聚", "聚类"} <= ok

    def test_short_query_needs_no_corroboration(self):
        """反向对照：整段 CJK <= 3 字时是完整短语，必须放行。

        否则「沙箱」「回滚」这种独立词永远搜不到 —— 那才是真的坏了。
        """
        assert "沙箱" in corroborated_tokens("沙箱", "容器化沙箱的目录结构")
        assert "回滚" in corroborated_tokens("回滚", "发布前必须确认有回滚方案")

    def test_ascii_tokens_always_corroborated(self):
        """ASCII 是整词，不存在跨词边界问题，一律算有佐证。"""
        assert "createpr" in corroborated_tokens("CreatePR 的用法", "CreatePR 用来开 PR")

    def test_discount_lowers_the_score(self, monkeypatch):
        """降权必须真的把分数压下来 —— 同查询同文档，只改系数。

        这里刻意**不断言绝对分数**：不传 idf、文档又短的时候，分数尺度
        和真实语料（4 篇文档、正文上千 token）完全不是一回事，
        拿 MIN_SCORE 去卡一个合成文档是假的断言。绝对门槛由下面
        那两个端到端用例负责。
        """
        from mewcode.tools.knowledge import backend as kbmod
        from mewcode.tools.knowledge.backend import CORROBORATION_DISCOUNT

        body = (
            "三种模式的护栏完全一样。真正承重的三条和谁推送无关。"
            "Grep 用来定位问题。把类型检查报错当无关忽略掉。"
        )

        def score_at(discount: float) -> float:
            monkeypatch.setattr(kbmod, "CORROBORATION_DISCOUNT", discount)
            return score_doc("完全无关的问题", title="T", tags=[], body=body)

        full = score_at(1.0)          # 等于没有这层防护
        discounted = score_at(CORROBORATION_DISCOUNT)
        assert discounted < full, f"降权没生效：{discounted} vs {full}"

    def test_end_to_end_unrelated_question_returns_nothing(self, tmp_path):
        """★ 端到端反例：文档里真的有「完全 / 无关 / 问题」，但必须搜不到。"""
        _write(
            tmp_path,
            ".mewcode/knowledge/readme.md",
            "# 运维 Agent\n\n三种模式的护栏完全一样。三条铁律和谁推送无关。\n"
            "Grep 用来定位问题。\n",
        )
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert b.search("完全无关的问题") == []

    def test_end_to_end_real_question_still_finds_it(self, tmp_path):
        """反向对照：同一篇文档，问它真的讲了的东西必须搜得到。"""
        _write(
            tmp_path,
            ".mewcode/knowledge/readme.md",
            "# 运维 Agent\n\n三条铁律：绝不推基线分支、绝不合并、验证先于推送。\n",
        )
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        hits = b.search("三条铁律")
        assert hits and hits[0].doc.doc_id == "readme"

    def test_negative_control_without_the_guard_it_would_hit(
        self, tmp_path, monkeypatch
    ):
        """★ 反向对照：关掉这层防护，上面那条端到端反例必须**失效**。

        没有这条，「搜不到」可能是因为语料里压根没这个词，
        而不是因为防护生效 —— 断言就是恒真的。
        """
        from mewcode.tools.knowledge import backend as kbmod

        _write(
            tmp_path,
            ".mewcode/knowledge/readme.md",
            "# 运维 Agent\n\n三种模式的护栏完全一样。三条铁律和谁推送无关。\n"
            "Grep 用来定位问题。\n",
        )
        monkeypatch.setattr(kbmod, "CORROBORATION_DISCOUNT", 1.0)
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        hits = b.search("完全无关的问题")
        assert hits, "关掉防护后本该出现的假命中没有出现，说明这组用例没在测该测的东西"


class TestCoverageGate:
    """覆盖度：长查询只命中一个词，算弱匹配（见 `min_matches_for`）。"""

    def test_short_queries_need_only_one_token(self):
        assert min_matches_for(tokenize("回滚")) == 1
        assert min_matches_for(tokenize("回滚 方案")) == 1

    def test_long_queries_need_two_tokens(self):
        assert min_matches_for(tokenize("数据库变更要不要回滚脚本")) == 2

    def test_coverage_gate_is_not_redundant_with_the_score_threshold(self):
        """★ 证明这道门真的在承重：这个命中的分数**够高**，挡住它的是覆盖度。

        没有这条对照的话，「搜不到」可能只是因为分数不够，
        覆盖度那道门是死代码也测不出来。
        """
        q = "沙箱 网络 隔离 策略 配置"
        sb = score_and_match(q, title="架构说明", tags=[], body="服务之间必须隔离。")
        assert sb.n_matched == 1, "这个用例要的就是「只命中一个词」"
        assert sb.score >= MIN_SCORE, (
            f"分数本该过线（{sb.score} >= {MIN_SCORE}）—— 这样才说明"
            "挡住它的是覆盖度而不是分数"
        )
        assert sb.n_matched < min_matches_for(tokenize(q))

    def test_end_to_end_long_query_with_one_token_finds_nothing(self, tmp_path):
        _write(
            tmp_path,
            ".mewcode/knowledge/a.md",
            "# 架构说明\n\ncontroller 层只做参数校验，服务之间必须隔离。\n",
        )
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert b.search("沙箱 网络 隔离 策略 配置") == []


class TestSnippet:
    def test_snippet_centers_on_the_hit_not_the_head(self):
        """★ 命中在文档末尾时，片段必须裁到末尾附近。

        从头截的话，模型看到片段也判断不出相关性。
        """
        body = "开头" + "无关内容。" * 200 + "这里才是错误码规范的核心"
        snip = make_snippet(body, tokenize("错误码"), width=120)
        assert "错误码" in snip, f"片段没包含命中词：{snip!r}"

    def test_snippet_marks_truncation(self):
        body = "前置" * 100 + "目标词" + "后置" * 100
        snip = make_snippet(body, tokenize("目标词"), width=60)
        assert snip.startswith("…")
        assert "目标词" in snip

    def test_empty_body(self):
        assert make_snippet("", tokenize("x")) == ""


# ---------------------------------------------------------------------------
# 三、Mock 后端
# ---------------------------------------------------------------------------


class TestMockBackend:
    def test_search_finds_builtin_docs(self):
        b = MockKnowledgeBackend()
        hits = b.search("数据库变更要不要回滚脚本")
        assert hits
        assert any("DATABASE" in h.doc.doc_id or "数据库" in h.doc.title for h in hits)

    def test_search_no_match_returns_empty(self):
        assert MockKnowledgeBackend().search("完全不相干的话题xyzzy") == []

    def test_read_known_and_unknown(self):
        b = MockKnowledgeBackend()
        assert "错误码" in b.read("STD-001")
        with pytest.raises(KnowledgeError, match="不存在"):
            b.read("NOPE")

    def test_read_truncates(self):
        b = MockKnowledgeBackend()
        out = b.read("STD-001", max_chars=10)
        assert "已截断" in out

    def test_list_docs_with_prefix(self):
        b = MockKnowledgeBackend()
        ids = [d.doc_id for d in b.list_docs("STD-")]
        assert ids == ["STD-001", "STD-002"]
        assert len(b.list_docs()) >= 4

    def test_describe_mentions_it_is_demo_data(self):
        assert "演示" in MockKnowledgeBackend().describe()


# ---------------------------------------------------------------------------
# 四、「没接入」必须可区分
# ---------------------------------------------------------------------------


class TestUnavailable:
    def test_require_backend_raises_not_empty(self):
        """★ 没接后端要抛错。

        返回空列表会让 agent 把「没接入」读成「内部规范里没有这条」，
        然后凭自己的习惯写代码，还以为符合标准。
        """
        with pytest.raises(KnowledgeUnavailable) as exc:
            require_backend(None)
        msg = str(exc.value)
        assert "不代表" in msg
        assert "没有接入" in msg

    def test_report_for_none_says_unavailable(self):
        note = knowledge_report(None)
        assert "未接入" in note
        assert "不代表" in note

    def test_report_for_backend_passthrough(self):
        assert "演示" in knowledge_report(MockKnowledgeBackend())


# ---------------------------------------------------------------------------
# 五、本地文档后端
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestLocalDocsBackend:
    def test_scans_project_knowledge_dir(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/standards/error.md",
               "# 错误处理规范\ntags: 规范, 异常\n\n所有接口必须返回结构化错误码。")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        hits = b.search("错误码规范")
        assert hits
        assert hits[0].doc.title == "错误处理规范"
        assert "规范" in hits[0].doc.tags

    def test_doc_id_from_relative_path(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/standards/error.md", "# 错误")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert [d.doc_id for d in b.list_docs()] == ["standards/error"]

    def test_extra_dirs(self, tmp_path):
        _write(tmp_path, "docs/arch.md", "# 服务分层架构\ncontroller 只做参数校验。")
        b = LocalDocsBackend(tmp_path, extra_dirs=["docs"], include_user_dir=False)
        assert b.search("分层架构")

    def test_hidden_dirs_skipped(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/.private/secret.md", "# 密钥规范 机密")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert b.search("机密") == []

    def test_non_text_suffixes_ignored(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/data.json", '{"规范": "不该被读"}')
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert b.search("规范") == []

    def test_title_falls_back_to_filename(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/db-migration-rules.md", "没有一级标题的正文")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        doc = b.list_docs()[0]
        assert doc.title == "db migration rules"

    def test_mtime_cache_picks_up_edits(self, tmp_path):
        """★ 缓存必须失效 —— agent 在工作过程中新写的规范要能被立刻搜到。

        这里用**不重叠的 ASCII 标记**（zzzalpha / zzzbeta）而不是中文词：
        `tokenize("新内容")` 会产生二元组「内容」，而正文「旧内容」也含「内容」，
        于是会合法命中 —— 那是二元组匹配的正常特性，不是缓存没失效。
        用中文写这个断言会变成一个假失败。
        """
        import os
        import time

        p = _write(tmp_path, ".mewcode/knowledge/x.md", "# 标题\nzzzalpha")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        assert b.search("zzzalpha"), "改之前就搜不到"
        assert not b.search("zzzbeta"), "改之前不该搜到"

        time.sleep(0.01)
        p.write_text("# 标题\nzzzbeta", encoding="utf-8")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 2))

        assert b.search("zzzbeta"), "文件改了但缓存没失效"
        assert not b.search("zzzalpha"), "改完之后旧内容还在"

    def test_read_unknown_lists_available(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/a.md", "# A")
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        with pytest.raises(KnowledgeError) as exc:
            b.read("nope")
        assert "a" in str(exc.value)

    def test_read_truncates_with_marker(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/big.md", "# H\n" + "内容" * 5000)
        b = LocalDocsBackend(tmp_path, include_user_dir=False)
        out = b.read("big", max_chars=100)
        assert "已截断" in out

    def test_describe_when_no_dirs(self, tmp_path):
        b = LocalDocsBackend(tmp_path / "empty", include_user_dir=False)
        assert "没有找到任何文档目录" in b.describe()

    def test_describe_when_has_docs(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/a.md", "# A")
        d = LocalDocsBackend(tmp_path, include_user_dir=False).describe()
        assert "LocalDocsBackend" in d
        assert "1 篇" in d


# ---------------------------------------------------------------------------
# 六、HTTP 后端
# ---------------------------------------------------------------------------


class _Rec:
    def __init__(self, payload, status=200, raw=None):
        self.payload, self.status, self.raw = payload, status, raw
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raw is not None:
            return httpx.Response(self.status, text=self.raw)
        return httpx.Response(self.status, json=self.payload)


def _client(rec: _Rec) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(rec), base_url="http://kb.internal")


DEFAULT_PAYLOAD = {
    "results": [
        {"id": "STD-9", "title": "日志脱敏规范", "snippet": "禁止打印手机号",
         "url": "http://kb/9", "tags": ["规范", "日志"], "updated": "2026-09-01"},
        {"id": "STD-8", "title": "接口命名规范", "snippet": "用 kebab-case",
         "url": "", "tags": "规范,命名", "updated": ""},
    ]
}


class TestHttpBackend:
    def _b(self, rec, **kw):
        return HttpKnowledgeBackend("http://kb.internal", client=_client(rec), **kw)

    def test_request_construction(self):
        rec = _Rec(0 and None or DEFAULT_PAYLOAD)
        self._b(rec).search("日志", limit=3)
        p = dict(rec.requests[-1].url.params)
        assert rec.requests[-1].url.path == "/search"
        assert p == {"q": "日志", "limit": "3"}

    def test_parses_default_shape(self):
        rec = _Rec(DEFAULT_PAYLOAD)
        hits = self._b(rec).search("规范")
        assert [h.doc.doc_id for h in hits] == ["STD-9", "STD-8"]
        assert hits[0].doc.title == "日志脱敏规范"
        assert hits[0].snippet == "禁止打印手机号"
        assert hits[0].doc.tags == ["规范", "日志"]

    def test_tags_as_comma_string_are_split(self):
        rec = _Rec(DEFAULT_PAYLOAD)
        hits = self._b(rec).search("规范")
        assert hits[1].doc.tags == ["规范", "命名"]

    def test_custom_response_path_and_fields(self):
        """字段映射：接自己的网关时只改配置，不改代码。"""
        payload = {"data": {"items": [{"docId": "X1", "name": "内部规范", "body": "片段"}]}}
        rec = _Rec(payload)
        b = self._b(rec, mapping={
            "response_path": "data.items", "id_field": "docId",
            "title_field": "name", "snippet_field": "body",
        })
        hits = b.search("规范")
        assert hits[0].doc.doc_id == "X1"
        assert hits[0].doc.title == "内部规范"

    def test_empty_results_is_not_an_error(self):
        rec = _Rec({"results": []})
        assert self._b(rec).search("不存在") == []

    def test_http_error_raises_not_empty(self):
        """★ 服务挂了必须报错，不能返回空。

        返回空会让 agent 把「检索服务故障」读成「内部没有这条规范」。
        """
        rec = _Rec({"e": 1}, status=500)
        with pytest.raises(KnowledgeError, match="500"):
            self._b(rec).search("x")

    def test_non_json_raises(self):
        rec = _Rec(None, raw="<html>oops</html>")
        with pytest.raises(KnowledgeError, match="不是 JSON"):
            self._b(rec).search("x")

    def test_missing_response_path_lists_top_level_keys(self):
        rec = _Rec({"hits": {"hits": []}})
        with pytest.raises(KnowledgeError) as exc:
            self._b(rec).search("x")
        msg = str(exc.value)
        assert "response_path" in msg
        assert "hits" in msg          # 告诉用户实际收到什么

    def test_results_not_a_list_raises(self):
        rec = _Rec({"results": {"nope": 1}})
        with pytest.raises(KnowledgeError, match="不是数组"):
            self._b(rec).search("x")

    def test_items_without_id_are_skipped(self):
        rec = _Rec({"results": [{"title": "没有 id"}, {"id": "ok", "title": "有 id"}]})
        hits = self._b(rec).search("x")
        assert [h.doc.doc_id for h in hits] == ["ok"]

    def test_read_without_read_path_is_a_clear_error(self):
        rec = _Rec(DEFAULT_PAYLOAD)
        with pytest.raises(KnowledgeError, match="只配了检索"):
            self._b(rec).read("STD-9")

    def test_read_with_read_path(self):
        rec = _Rec({"content": "完整正文"})
        b = self._b(rec, read_path="/docs/{id}", read_id_field="content")
        assert b.read("STD-9") == "完整正文"
        assert rec.requests[-1].url.path == "/docs/STD-9"

    def test_read_missing_field_lists_keys(self):
        rec = _Rec({"body": "在别的字段里"})
        b = self._b(rec, read_path="/docs/{id}", read_id_field="content")
        with pytest.raises(KnowledgeError, match="body"):
            b.read("X")

    def test_read_truncates(self):
        rec = _Rec({"content": "字" * 500})
        b = self._b(rec, read_path="/docs/{id}")
        assert "已截断" in b.read("X", max_chars=50)

    def test_list_docs_when_unsupported_says_so(self):
        rec = _Rec({"results": []})
        rec2 = _Rec({"e": 1}, status=404)
        b = self._b(rec2)
        with pytest.raises(KnowledgeError, match="列出全部文档"):
            b.list_docs()

    def test_list_docs_when_supported(self):
        rec = _Rec(DEFAULT_PAYLOAD)
        docs = self._b(rec).list_docs()
        assert len(docs) == 2

    def test_requires_base_url(self):
        with pytest.raises(KnowledgeError, match="需要 base_url"):
            HttpKnowledgeBackend("")

    def test_trust_env_defaults_off(self):
        """和运维后端同样的坑：httpx 在 Windows 上从注册表读系统代理，
        内网地址会被塞进代理拿到一个没线索的 502。"""
        b = HttpKnowledgeBackend("http://kb.internal")
        try:
            assert b._client.trust_env is False
        finally:
            b.close()

    def test_describe_mentions_read_support(self):
        b = self._b(_Rec(DEFAULT_PAYLOAD), read_path="/docs/{id}")
        assert "支持" in b.describe()
        b2 = self._b(_Rec(DEFAULT_PAYLOAD))
        assert "不支持" in b2.describe()


# ---------------------------------------------------------------------------
# 七、工厂
# ---------------------------------------------------------------------------


class TestFactory:
    def test_none_spec_returns_none(self):
        assert build_knowledge_backend(None, ".") is None
        assert build_knowledge_backend({}, ".") is None

    def test_mock(self):
        assert isinstance(
            build_knowledge_backend({"kind": "mock"}, "."), MockKnowledgeBackend
        )

    def test_local(self, tmp_path):
        b = build_knowledge_backend({"kind": "local"}, str(tmp_path))
        assert isinstance(b, LocalDocsBackend)

    def test_http(self):
        b = build_knowledge_backend(
            {"kind": "http", "base_url": "http://kb.internal"}, "."
        )
        assert isinstance(b, HttpKnowledgeBackend)
        b.close()

    def test_http_token_from_env(self):
        b = build_knowledge_backend(
            {"kind": "http", "base_url": "http://kb.internal", "token_env": "KB_T"},
            ".", env={"KB_T": "s3cret"},
        )
        assert b._client.headers["Authorization"] == "Bearer s3cret"
        b.close()

    def test_http_missing_token_env_fails_loudly(self):
        with pytest.raises(KnowledgeError, match="KB_MISSING"):
            build_knowledge_backend(
                {"kind": "http", "base_url": "http://kb.internal",
                 "token_env": "KB_MISSING"},
                ".", env={},
            )

    def test_unknown_kind(self):
        with pytest.raises(KnowledgeError, match="不认识的知识库类型"):
            build_knowledge_backend({"kind": "confluence"}, ".")

    def test_http_without_base_url(self):
        with pytest.raises(KnowledgeError, match="需要 base_url"):
            build_knowledge_backend({"kind": "http"}, ".")


# ---------------------------------------------------------------------------
# 八、配置校验
# ---------------------------------------------------------------------------


class TestValidateKnowledge:
    def test_absent_is_none(self):
        assert validate_knowledge(None) is None
        assert validate_toolset(None)["knowledge"] is None

    def test_defaults_to_local(self):
        out = validate_knowledge({})
        assert out["kind"] == "local"
        assert out["include_user_dir"] is True
        assert out["trust_env"] is False

    def test_http_requires_base_url(self):
        with pytest.raises(ConfigError, match="base_url"):
            validate_knowledge({"kind": "http"})

    def test_unknown_kind_rejected(self):
        with pytest.raises(ConfigError, match="kind must be one of"):
            validate_knowledge({"kind": "confluence"})

    def test_extra_dirs_string_becomes_list(self):
        assert validate_knowledge({"extra_dirs": "docs"})["extra_dirs"] == ["docs"]

    def test_extra_dirs_bad_type(self):
        with pytest.raises(ConfigError, match="extra_dirs"):
            validate_knowledge({"extra_dirs": [1, 2]})

    def test_mapping_must_be_str_to_str(self):
        with pytest.raises(ConfigError, match="mapping"):
            validate_knowledge({"mapping": {"a": 1}})

    def test_timeout_positive(self):
        with pytest.raises(ConfigError, match="timeout"):
            validate_knowledge({"timeout": 0})

    def test_wired_through_toolset(self):
        out = validate_toolset({"knowledge": {"kind": "mock"}})
        assert out["knowledge"]["kind"] == "mock"


# ---------------------------------------------------------------------------
# 九、工具层
# ---------------------------------------------------------------------------


class TestKnowledgeTools:
    def _reg(self, backend):
        from mewcode.tools.knowledge.tools import register_knowledge_tools

        reg = create_default_registry()
        register_knowledge_tools(reg, backend)
        return reg

    async def _call(self, reg, name, **kw):
        tool = reg.get(name)
        assert tool is not None, f"{name} 没注册"
        return await tool.execute(tool.params_model(**kw))

    async def test_search_returns_snippets_not_full_text(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "SearchKnowledge", query="数据库迁移")
        assert not r.is_error
        assert "STD-002" in r.output
        assert "ReadKnowledge" in r.output      # 引导下一步

    async def test_search_no_match_is_not_an_error_but_says_so(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "SearchKnowledge", query="zzzz不存在的主题")
        assert not r.is_error
        assert "知识库中没有" in r.output
        assert "不表示" in r.output

    async def test_search_without_backend_is_error(self):
        reg = self._reg(None)
        r = await self._call(reg, "SearchKnowledge", query="规范")
        assert r.is_error
        assert "没有接入" in r.output
        assert "不代表" in r.output

    async def test_read_returns_body(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "ReadKnowledge", doc_id="STD-001")
        assert not r.is_error
        assert "结构化错误码" in r.output

    async def test_read_unknown_id_is_error(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "ReadKnowledge", doc_id="NOPE")
        assert r.is_error

    async def test_read_rejects_nonpositive_max_chars(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "ReadKnowledge", doc_id="STD-001", max_chars=0)
        assert r.is_error

    async def test_list_docs(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "ListKnowledgeDocs")
        assert "STD-001" in r.output

    async def test_list_docs_empty(self):
        reg = self._reg(MockKnowledgeBackend())
        r = await self._call(reg, "ListKnowledgeDocs", prefix="ZZZ")
        assert not r.is_error
        assert "没有文档" in r.output

    def test_all_tools_are_read_only(self):
        reg = self._reg(MockKnowledgeBackend())
        for n in ("SearchKnowledge", "ReadKnowledge", "ListKnowledgeDocs"):
            assert reg.get(n).category == "read", n


# ---------------------------------------------------------------------------
# 十、装配（toolset）
# ---------------------------------------------------------------------------


class TestToolsetWiring:
    def test_not_configured_changes_nothing(self, tmp_path):
        """不配置 knowledge 时，工具集和以前完全一样。"""
        reg = create_default_registry()
        before = {t.name for t in reg.list_tools()}
        asm = assemble_toolset(reg, ToolsetConfig(), agent=None, work_dir=tmp_path)
        assert {t.name for t in reg.list_tools()} == before
        assert asm.knowledge is None

    def test_configured_registers_three_tools(self, tmp_path):
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.knowledge = KnowledgeConfig(kind="mock")
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        names = {t.name for t in reg.list_tools()}
        for n in ("SearchKnowledge", "ReadKnowledge", "ListKnowledgeDocs"):
            assert n in names
        assert asm.knowledge is not None

    def test_prompt_note_mentions_knowledge_and_urges_searching(self, tmp_path):
        """清单要进 prompt —— 否则 agent 不知道有规范可查，就凭习惯写了。"""
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.knowledge = KnowledgeConfig(kind="mock")
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "知识库" in asm.prompt_note
        assert "SearchKnowledge" in asm.prompt_note
        assert "先" in asm.prompt_note

    def test_local_backend_end_to_end_through_toolset(self, tmp_path):
        _write(tmp_path, ".mewcode/knowledge/std.md",
               "# 提交信息规范\ntags: 规范\n\n提交信息必须用 conventional commits。")
        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.knowledge = KnowledgeConfig(kind="local", include_user_dir=False)
        assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)

        import asyncio

        tool = reg.get("SearchKnowledge")
        r = asyncio.run(tool.execute(tool.params_model(query="提交信息规范")))
        assert not r.is_error
        assert "conventional commits" in r.output

    def test_combines_with_ops_note(self, tmp_path):
        from mewcode.config import OpsBackendConfig

        reg = create_default_registry()
        cfg = ToolsetConfig()
        cfg.ops = [OpsBackendConfig(kind="mock", capability="mock")]
        cfg.knowledge = KnowledgeConfig(kind="mock")
        asm = assemble_toolset(reg, cfg, agent=None, work_dir=tmp_path)
        assert "运维后端能力清单" in asm.prompt_note
        assert "知识库" in asm.prompt_note

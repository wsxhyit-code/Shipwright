"""`extract_summary` 的纯函数测试。

这个函数是"摘要产出 → 进上下文"的最后一道闸门，只靠标签定位，
没有任何结构性校验。下面这条 xfail 记录的就是它的真实缺陷。
"""
from __future__ import annotations

import pytest

from mewcode.context.manager import SUMMARY_PROMPT, extract_summary


def test_strips_analysis_when_both_tags_present():
    out = extract_summary("<analysis>草稿，本该被丢弃</analysis>\n<summary>正式摘要</summary>")
    assert out == "正式摘要"
    assert "草稿" not in out


def test_keeps_inner_newlines():
    out = extract_summary("<analysis>x</analysis><summary>第一行\n第二行</summary>")
    assert out == "第一行\n第二行"


def test_returns_raw_when_no_tags_at_all():
    """没有任何标签时走兜底，原样返回——这是「模型没按格式答」时的降级路径。"""
    assert extract_summary("就是一段普通文本") == "就是一段普通文本"


def test_returns_raw_when_summary_tag_unclosed():
    """只有开标签没有闭标签：整段返回，开标签本身也被带进上下文。"""
    out = extract_summary("<summary>没闭合的摘要")
    assert out == "<summary>没闭合的摘要"


def test_analysis_must_not_leak_when_summary_tag_missing():
    """修复记录：模板声称 <analysis> 会被丢弃，但旧实现只在两个标签都在时才剥离。

    模型漏输出 `<summary>` 时（截断 / 格式漂移），本该丢弃的推演会被整段塞进
    上下文 —— 既浪费 token，又把草稿式推理留在了历史里。现在兜底会先剥掉
    `<analysis>` 再返回。**这条从 xfail 转成了正式断言。**
    """
    raw = "<analysis>这段推演本该被丢弃</analysis>"
    out = extract_summary(raw)
    assert "这段推演本该被丢弃" not in out
    assert out == ""


def test_unclosed_analysis_tag_is_dropped():
    """未闭合的 <analysis>：从它开始到结尾都丢（那部分本就标注了会被丢弃）。"""
    assert extract_summary("<analysis>没闭合的草稿，后面跟着一堆推演") == ""


def test_plain_text_without_tags_is_preserved():
    """兜底路径不能一律丢空 —— 没有标签的正常纯文本仍要保留。"""
    assert extract_summary("就是一段普通文本") == "就是一段普通文本"


def test_analysis_stripped_but_remainder_kept():
    assert extract_summary("<analysis>草稿</analysis>剩下的话") == "剩下的话"


def test_summary_prompt_declares_nine_sections():
    """模板契约：9 个栏目一个都不能少。少了哪个，就少一类信息被兜住。"""
    for n in range(1, 10):
        assert f"{n}." in SUMMARY_PROMPT, f"SUMMARY_PROMPT 缺少第 {n} 栏"


def test_summary_prompt_mandates_verbatim_user_messages():
    """第 6 条是这次模板修复的核心：用户原话必须原文保留、不可改写。

    没有它，用户的数字/否定/措辞会被摘要在"概括"中全部抹掉。
    """
    assert "所有用户消息" in SUMMARY_PROMPT
    assert "原文保留" in SUMMARY_PROMPT
    assert "不可改写" in SUMMARY_PROMPT

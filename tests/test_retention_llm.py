"""真实 LLM 保留率评测（需要 API，默认跳过）。

    pytest -m llm tests/test_retention_llm.py

这里才是**测出真实数字**的地方。`test_retention_pipeline.py` 里的伪造摘要器
只能验证流程，用它得出的百分比是自证，没有意义。

成本估算（`--probes-per-type` 默认 1，即每条用例 7 个探针 + 1 个陷阱）：
    1 条摘要调用 + 4 变体 × 8 探针 × trials 次答题调用
  默认 trials=1 → 约 33 次调用；trials=3 → 约 97 次调用。
建议先用 1 条用例 / 1 次采样跑通，再放大。
"""
from __future__ import annotations

import pytest

from tests.retention.answerers import ABSENT, LLMAnswerer
from tests.retention.generator import build_case
from tests.retention.harness import compute_metrics, format_report, run_case
from tests.retention.summarizers import RealSummarizer

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def real_client():
    """按项目配置构造真实 client（沿用 .mewcode/config.yaml 的 provider）。"""
    from mewcode.client import create_client
    from mewcode.config import load_config

    cfg = load_config()
    return create_client(cfg.providers[0])


@pytest.fixture(scope="module")
def llm_case():
    return build_case("ret-llm-0", target_tokens=12_000, probes_per_type=1)


async def test_real_summary_retention(real_client, llm_case, session_dir):
    """真实保留率。断言很宽松——这条用例的作用是**产出数字**，不预设结论。"""
    summarizer = RealSummarizer(real_client)
    answerer = LLMAnswerer(real_client)

    run = await run_case(llm_case, summarizer, answerer, session_dir)
    assert run.valid, run.invalid_reason

    print(format_report([run], title=f"真实 LLM 保留率（{summarizer.name}）"))

    control = compute_metrics([run], "control")
    # 对照组是我们唯一可以硬断言的东西：不压缩时模型必须答得出
    assert control.irr == 1.0, (
        f"对照组只有 {control.irr:.1%}，说明探针有歧义或答案存在先验泄漏，"
        "本次测量的其余数字都不可信"
    )

    summary = compute_metrics([run], "summary")
    # 不做"必须很高"的断言：这个数字就是要被观测的
    assert 0.0 <= summary.irr <= 1.0
    assert summary.compression_ratio > 0.0


async def test_real_answerer_respects_absence(real_client, llm_case, session_dir):
    """真实答题器在信息缺失时必须承认"未提及"，而不是编造。

    这是幻觉率的直接来源：如果模型在摘要没保留的情况下也敢答具体值，
    那 IRR 就会被幻觉抬高，指标失效。
    """
    answerer = LLMAnswerer(real_client)
    trap = next(p for p in llm_case.probes if p.trap)

    # 故意给一个**空上下文**，模型没有依据可答
    answer = await answerer.answer([], trap)
    assert ABSENT in answer or "未提及" in answer or "没有" in answer, (
        f"空上下文下模型仍给出了具体值：{answer!r} —— 幻觉率指标需重新设计"
    )

"""真实 LLM 集成测试：独立验证子 agent 是否真的能抓到问题。

    pytest -m llm tests/test_llm_integration.py -v

**这些用例之前从来没有跑过** —— 独立验证是整个方向的核心卖点，
但在它之前的所有测试里，验证者都是 `make_static_verifier`（返回固定字符串的假货）。
也就是说：**"另一个 agent 会去审你的代码"这件事从没被真实执行过。**

这里测三件事：

  1. `make_subagent_verifier` 能真的跑起来（构造参数、只读注册表都对）
  2. 它**会输出 `VERDICT:` 结论**（我的正则依赖这个格式约定）
  3. **它能抓到植入的 bug**（这才是它值不值得存在的唯一标准）

代价：每个用例要跑一整轮独立 agent，几十秒到几分钟。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.tools.agent_verify import (
    READ_ONLY_TOOL_NAMES,
    build_readonly_registry,
    make_subagent_verifier,
    parse_verdict,
)

pytestmark = pytest.mark.llm


# ---------------------------------------------------------------------------
# 搭一个最小的父 agent（照 __main__.py 的 _run_prompt 组装）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parent_agent(tmp_path_factory=None):
    from mewcode.agent import Agent
    from mewcode.client import create_client
    from mewcode.config import load_config
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )
    from mewcode.tools import create_default_registry

    cfg = load_config()
    provider = cfg.providers[0]
    client = create_client(provider)

    # 用一个固定的工作目录，方便控制"被审的代码"
    work = Path(__file__).resolve().parent / ".tmp-llm-verify"
    work.mkdir(parents=True, exist_ok=True)

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(work)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    return Agent(
        client=client,
        registry=create_default_registry(),
        permission_checker=checker,
        protocol=provider.protocol,
        work_dir=str(work),
        max_iterations=20,
    )


@pytest.fixture
def workdir(parent_agent) -> Path:
    d = Path(parent_agent.work_dir)
    for p in d.iterdir():
        if p.is_file():
            p.unlink()
    return d


# ---------------------------------------------------------------------------
# 1. 只读注册表：不需要 LLM 也能验，但放在这里做前置
# ---------------------------------------------------------------------------


def test_readonly_registry_has_no_mutating_tools():
    from mewcode.tools import create_default_registry

    ro = build_readonly_registry(create_default_registry())
    names = {t.name for t in ro.list_tools()}
    assert names, "只读注册表是空的，验证者会什么都做不了"
    for forbidden in ("WriteFile", "EditFile", "Bash"):
        assert forbidden not in names, f"{forbidden} 不该出现在只读注册表里"
    assert names <= READ_ONLY_TOOL_NAMES


# ---------------------------------------------------------------------------
# 2. 验证者能跑起来并给出结论
# ---------------------------------------------------------------------------


async def test_verifier_runs_and_emits_verdict(parent_agent, workdir):
    (workdir / "util.py").write_text(
        "def divide(a, b):\n    return a / b\n", encoding="utf-8"
    )
    runner = make_subagent_verifier(parent_agent)
    raw = await runner(
        "新增了 util.py 里的 divide 函数，用于做除法。"
        "改动摘要：util.py | 2 ++\n 1 file changed, 2 insertions(+)"
    )

    assert raw and raw.strip(), "验证者没有产出任何输出"
    verdict = parse_verdict(raw)
    # 没给 VERDICT 会被 parse_verdict 判成 FAIL（fail-closed），
    # 但那说明**格式约定没被遵守**，需要调整提示词而不是当成"审出问题了"
    assert "VERDICT" in raw.upper(), (
        f"验证者没有输出 VERDICT 结论。原始输出：\n{raw[:800]}"
    )
    assert verdict.raw == raw


# ---------------------------------------------------------------------------
# 3. 它真的能抓到 bug 吗 —— 这才是它值不值得存在的唯一标准
# ---------------------------------------------------------------------------


BROKEN_CODE = '''\
"""用户配置读取。"""


def get_profile(user):
    """返回用户的 profile。"""
    return user["profile"]


def get_tier(user):
    """返回用户的等级。

    需求：user 没有 profile 时应该返回 "GUEST"，不能抛异常。
    """
    profile = get_profile(user)
    return profile["tier"]          # ← BUG: profile 可能是 None
'''

FIXED_CODE = '''\
"""用户配置读取。"""

GUEST_TIER = "GUEST"


def get_profile(user):
    """返回用户的 profile，没有则返回 None。"""
    return user.get("profile")


def get_tier(user):
    """返回用户的等级；没有 profile 时返回 GUEST。"""
    profile = get_profile(user)
    if profile is None:
        return GUEST_TIER
    return profile["tier"]
'''


async def test_verifier_catches_the_planted_bug(parent_agent, workdir):
    """★ 核心用例：植入一个"缺少判空"的 bug，验证者必须判 FAIL。

    如果它在这里判 PASS，那整套"独立验证"就是摆设 —— 它只能给实现者的
    工作盖章，抓不到真问题。
    """
    (workdir / "profile.py").write_text(BROKEN_CODE, encoding="utf-8")
    runner = make_subagent_verifier(parent_agent)
    raw = await runner(
        "改动了 profile.py 的 get_tier：需求是「user 没有 profile 时返回 GUEST」，"
        "但实现看起来是直接取 profile['tier']。\n"
        "改动摘要：profile.py | 4 ++\n 1 file changed, 4 insertions(+)"
    )

    verdict = parse_verdict(raw)
    assert not verdict.passed, (
        "验证者没有抓到「缺判空」这个明显的 bug —— 独立验证形同虚设。\n"
        f"验证者原话：\n{raw[:1200]}"
    )
    # 理由里应该点到"判空/None/GUEST"这类关键词，说明它真的读懂了代码
    reason = (verdict.reason or "").lower()
    assert any(k in reason for k in ("none", "判空", "null", "guest", "keyerror")), (
        f"判了 FAIL 但理由看不出读懂了代码：\n{verdict.reason[:600]}"
    )


async def test_verifier_passes_a_correct_implementation(parent_agent, workdir):
    """反向用例：实现是对的（有判空）时，不该乱判 FAIL。

    它必须**有区分能力** —— 一个永远判 FAIL 的验证者和不存在的验证者一样没用，
    而且会把流程彻底堵死。
    """
    (workdir / "profile.py").write_text(FIXED_CODE, encoding="utf-8")
    runner = make_subagent_verifier(parent_agent)
    raw = await runner(
        "改动了 profile.py 的 get_tier：需求是「user 没有 profile 时返回 GUEST」。\n"
        "改动摘要：profile.py | 6 ++\n 1 file changed, 6 insertions(+)"
    )

    verdict = parse_verdict(raw)
    assert verdict.passed, (
        f"实现是对的却被判 FAIL —— 验证者会误伤。\n验证者原话：\n{raw[:1200]}"
    )


async def test_verifier_cannot_modify_files(parent_agent, workdir):
    """独立性保证：验证者**物理上**改不了文件。

    它的注册表里根本没有写工具，所以即使它"想"改也做不到。
    """
    target = workdir / "profile.py"
    target.write_text(BROKEN_CODE, encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    runner = make_subagent_verifier(parent_agent)
    await runner("profile.py 里 get_tier 缺少判空。请审查。")

    assert target.read_text(encoding="utf-8") == before, "验证者改动了文件！"

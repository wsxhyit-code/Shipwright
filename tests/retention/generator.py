"""半结构化对话生成器：把探针事实填进模板，再撑到目标长度。

撑长度不是凑数——`auto_compact` 有硬前置条件，不满足就**静默不压缩**
（`context/manager.py:698-702`），测试会假通过：

  - `keep_start > 0`                → 至少 6 条消息
  - 前缀 ≥ MIN_SUMMARIZE_PREFIX_TOKENS(2000) tokens → 约 7000 字符

尾部固定留 6 条 filler：`_compute_keep_start_index` 会因 MIN_KEEP_MESSAGES(5)
在倒数第 5 条停住，于是 keep 窗口 = 最后 5 条，探针自然落在 prefix 区。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock, estimate_tokens

from tests.retention.schema import PROBE_TYPES, Probe, ProbeCase
from tests.retention.templates import ProbeSpec, build_probe_spec, build_trap_spec

# 真实 agent 会话的 token 分布（估计）：工具结果是大头。
# 依据：本仓库 .mewcode/session/tool-results/ 下 9 个已持久化输出合计 687KB，
# 全部来自 tool_result；replacement_records.jsonl 22KB 同样是 tool_result；
# 逐条读过的 session_20260623_092723_x5pw.jsonl 里，一条子 agent 报告就占 8KB，
# 而 user 消息基本是一两行。
# 对比：默认的 filler 是 99% user，严重失真，会让所有摘要策略看起来都在膨胀。
REALISTIC_MIX = {"user": 0.15, "assistant": 0.20, "tool_result": 0.65}

# 一轮真实 agent 交互的字符预算，按上面的比例分配
_RT_USER_CHARS = 320
_RT_ASSISTANT_CHARS = 620
_RT_TOOL_RESULT_CHARS = 2400
_RT_TAIL_ACK_CHARS = 200

# 探针要落在 prefix 区，尾部必须留够 filler。5 条是 keep 窗口，再多留 1 条缓冲。
_TAIL_FILLER_MESSAGES = 6
_HEAD_FILLER_MESSAGES = 2
# keep 区对照组里最多能埋几个探针（keep 窗口只有 5 条消息）
_MAX_KEEP_PROBES = 4

# 刻意避开探针会问到的关键词（端口/并发/上限/文件/函数/路径/命令），
# 否则 filler 可能"意外回答"了陷阱探针，让 absent 判分失真。
_FILLER_SENTENCES = (
    "我先把当前的模块划分梳理了一遍，整体分成接入、编排、落盘三层，层与层之间只通过显式的数据结构传递。",
    "日志这块我倾向于统一收敛到一个入口，避免每个模块各自拼格式，后面排查问题时口径能对上。",
    "缓存策略我暂时按最保守的来，先保证正确性，等压测数据出来再决定要不要加一层本地缓存。",
    "现有的抽象层级有点多，中间那层几乎没有自己的逻辑，只是把参数换个名字往下传，考虑合并掉。",
    "配置项散落在好几个地方，读起来要跳来跳去，长期看应该收敛成一份声明式的描述。",
    "错误处理目前是就地捕获后就地打日志，缺少统一的归类，出问题时不太好按类型统计。",
    "测试覆盖偏薄，主要集中在正常流程上，边界条件基本靠人工验证，这块要想办法补。",
    "我对比了两种组织的写法，一种是按层次铺开，一种是按功能切分，各有取舍，先按现阶段的情况选一个。",
    "依赖的方向需要理顺，现在存在双向引用，结构上是绕的，改动容易漏。",
    "数据流的中间态没有明确的所有者，谁都可以改，这种隐式约定时间长了会变成坑。",
    "对外暴露的接口我尽量收窄，只留真正需要的那几个，其余走内部调用。",
    "监控埋点先加在最关键的几个环节上，剩下等主链路稳定了再补，避免一次性铺太开。",
)

_ACKS = (
    "明白，我记下了。",
    "好的，收到。",
    "嗯，这点我注意一下。",
    "行，先按这个来。",
    "了解，我继续看。",
)


def _filler_message(rng: random.Random, min_chars: int = 900) -> Message:
    buf: list[str] = []
    total = 0
    while total < min_chars:
        s = rng.choice(_FILLER_SENTENCES)
        buf.append(s)
        total += len(s) + 1
    return Message(role="user", content="".join(buf))


def _ack(role: str, idx: int) -> Message:
    """给一条来自 `role` 的消息生成一条简短回应，让对话看起来自然。"""
    reply_role = "user" if role == "assistant" else "assistant"
    r = random.Random(f"ack-{idx}")
    return Message(role=reply_role, content=r.choice(_ACKS))


def _fill_to(rng: random.Random, min_chars: int) -> str:
    buf: list[str] = []
    total = 0
    while total < min_chars:
        s = rng.choice(_FILLER_SENTENCES)
        buf.append(s)
        total += len(s) + 1
    return "".join(buf)


def _tool_output(rng: random.Random, min_chars: int) -> str:
    """伪造一段看起来像 ReadFile 返回的工具输出（带行号）。"""
    lines: list[str] = []
    total = 0
    i = 1
    while total < min_chars:
        s = rng.choice(_FILLER_SENTENCES)
        lines.append(f"{i}\t{s}")
        total += len(s) + 8
        i += 1
    return "\n".join(lines)


def _realistic_turn(rng: random.Random, i: int) -> list[Message]:
    """一轮真实的 agent 交互，角色与 token 占比贴近线上：

        user 短问  →  assistant 文本 + tool_use  →  大块 tool_result  →  assistant 小结

    刻意带上 ToolUseBlock / ToolResultBlock，这样才会真正走到
    `_align_keep_start_to_tool_pair` 的配对对齐逻辑，
    `_message_chars` 也才会把 tool 参数和结果算进 token 估算。
    """
    call_id = f"call_filler_{i:04d}"
    return [
        Message(role="user", content=_fill_to(rng, _RT_USER_CHARS)),
        Message(
            role="assistant",
            content=_fill_to(rng, _RT_ASSISTANT_CHARS // 2),
            tool_uses=[
                ToolUseBlock(
                    tool_use_id=call_id,
                    tool_name="ReadFile",
                    arguments={"file_path": f"src/mod_{i:03d}.py"},
                )
            ],
        ),
        Message(
            role="user",
            content="",
            tool_results=[
                ToolResultBlock(
                    tool_use_id=call_id,
                    content=_tool_output(rng, _RT_TOOL_RESULT_CHARS),
                )
            ],
        ),
        Message(role="assistant", content=_fill_to(rng, _RT_TAIL_ACK_CHARS)),
    ]


def _filler_unit(rng: random.Random, i: int, realistic: bool) -> list[Message]:
    if realistic:
        return _realistic_turn(rng, i)
    return [_filler_message(rng, 1100), _ack("user", i)]


def text_of(messages: list[Message]) -> str:
    """把对话拍平成文本，**包含工具参数与工具结果**。

    自检要扫的是"期望值有没有在这个对话里意外复现"，而 realistic 模式下
    大部分内容在 tool_result 里，只看 content 会漏掉。
    """
    parts: list[str] = []
    for m in messages:
        parts.append(m.content)
        for tu in m.tool_uses:
            parts.append(f"{tu.tool_name} {tu.arguments}")
        for tr in m.tool_results:
            parts.append(tr.content)
    return "\n".join(parts)


def _make_probe(case_id: str, spec: ProbeSpec, seq: int, zone: str) -> Probe:
    return Probe(
        probe_id=f"{case_id}-{spec.type}-{seq}",
        type=spec.type,
        fact=spec.fact,
        question=spec.question,
        expected=spec.expected,
        grading=spec.grading,  # type: ignore[arg-type]
        zone=zone,  # type: ignore[arg-type]
    )


def build_case(
    case_id: str,
    target_tokens: int = 16_000,
    probes_per_type: int = 1,
    with_trap: bool = True,
    probes_in_tail: bool = False,
    realistic: bool = False,
) -> ProbeCase:
    """构造一条用例。

    probes_in_tail=True 时把探针埋进 keep 窗口，用于验证排除法里
    「这条信息本来就不归摘要管」的分支：摘要变体应该丢、keep 变体应该保住。

    realistic=True 时 filler 使用真实会话的角色分布（tool_result 占大头），
    默认的 99%-user 构造会严重高估摘要的膨胀程度。
    """
    rng = random.Random(f"filler-{case_id}")
    shared: dict[str, object] = {}
    msgs: list[Message] = []
    probes: list[Probe] = []

    for i in range(_HEAD_FILLER_MESSAGES):
        msgs.extend(_filler_unit(rng, i, realistic))

    # ── 生成候选探针事实 ─────────────────────────────────────
    specs: list[ProbeSpec] = []
    seq = 0
    for ptype in PROBE_TYPES:
        for _ in range(probes_per_type):
            specs.append(build_probe_spec(ptype, case_id, seq, shared))
            seq += 1

    trap_spec = build_trap_spec(case_id, seq, shared) if with_trap else None

    # ── 决定埋在哪里 ─────────────────────────────────────────
    if probes_in_tail:
        # keep 窗口只有 5 条，能埋的数量有限；只把真正埋进去的登记为探针，
        # 否则会出现"事实根本没在对话里出现过"的幽灵探针。
        specs = specs[:_MAX_KEEP_PROBES]
        zone = "keep"
    else:
        zone = "prefix"

    for i, spec in enumerate(specs):
        probes.append(_make_probe(case_id, spec, i, zone))
    if trap_spec is not None:
        probes.append(
            Probe(
                probe_id=f"{case_id}-trap",
                type="trap",
                fact=trap_spec.fact,
                question=trap_spec.question,
                expected=trap_spec.expected,
                grading="absent",
                zone="prefix",
                trap=True,
            )
        )

    # prefix 区：事实 + 回应成对插入
    if not probes_in_tail:
        for i, spec in enumerate(specs):
            msgs.append(Message(role=spec.role, content=spec.fact))
            msgs.append(_ack(spec.role, i))

    # ── 撑到目标长度 ─────────────────────────────────────────
    tail_budget_tokens = _TAIL_FILLER_MESSAGES * 300
    budget = max(0, target_tokens - tail_budget_tokens)
    guard = 0
    while estimate_tokens(msgs) < budget and guard < 400:
        msgs.extend(_filler_unit(rng, 1000 + guard, realistic))
        guard += 1

    # ── 尾部（keep 窗口）──────────────────────────────────────
    tail: list[Message]
    if probes_in_tail:
        # keep 区对照组需要逐条控制埋点位置，用简单 filler
        tail = [_filler_message(rng, 1100)]
        for spec in specs:
            tail.append(Message(role=spec.role, content=spec.fact))
        while len(tail) < _TAIL_FILLER_MESSAGES:
            tail.append(_filler_message(rng, 900))
    elif realistic:
        tail = []
        while len(tail) < _TAIL_FILLER_MESSAGES:
            tail.extend(_realistic_turn(rng, 9000 + len(tail)))
    else:
        tail = [_filler_message(rng, 1100)]
        while len(tail) < _TAIL_FILLER_MESSAGES:
            tail.append(_filler_message(rng, 900))
    msgs.extend(tail)

    case = ProbeCase(
        case_id=case_id,
        target_tokens=target_tokens,
        messages=msgs,
        probes=probes,
    )
    self_check(case)
    return case


def self_check(case: ProbeCase) -> None:
    """守住四条会让结果失真的底线。"""
    # 1) 事实值必须在用例内唯一——否则无法归因到具体探针
    expected_values = [p.expected for p in case.probes if p.expected]
    assert len(expected_values) == len(set(expected_values)), (
        f"{case.case_id}: 探针期望值重复，无法归因"
    )

    # 2) expected 不能出现在 question 里——否则判分自我实现
    for p in case.probes:
        if p.expected:
            assert p.expected not in p.question, (
                f"{p.probe_id}: expected 出现在 question 中，判分失效"
            )

    # 3) 每个探针的事实必须真的在对话里出现过（陷阱探针除外）
    body = text_of(case.messages)
    for p in case.probes:
        if p.trap:
            continue
        assert p.expected in body, f"{p.probe_id}: 事实没有真正埋进对话"
        assert body.count(p.expected) == 1, (
            f"{p.probe_id}: 期望值在对话中出现 {body.count(p.expected)} 次，"
            f"可能被 filler 意外复现"
        )

    # 4) 长度必须够触发压缩，否则 auto_compact 会静默返回 None
    assert case.total_tokens >= 4_000, (
        f"{case.case_id}: 仅 {case.total_tokens} tokens，不足以触发压缩"
    )


def build_dataset(
    sizes: tuple[int, ...] = (8_000, 16_000, 32_000),
    cases_per_size: int = 1,
    probes_per_type: int = 1,
    with_trap: bool = True,
    realistic: bool = False,
) -> list[ProbeCase]:
    """长度扫描：保留率是长度的函数，报一个数没有意义，要报一条曲线。"""
    cases: list[ProbeCase] = []
    for size in sizes:
        for i in range(cases_per_size):
            cases.append(
                build_case(
                    f"ret{size // 1000}k-{i}",
                    target_tokens=size,
                    probes_per_type=probes_per_type,
                    with_trap=with_trap,
                    realistic=realistic,
                )
            )
    # keep 区对照组：验证「这条信息本来就不归摘要管」的排除法分支
    cases.append(
        build_case(
            "ret-keepzone-0",
            target_tokens=12_000,
            probes_per_type=1,
            with_trap=False,
            probes_in_tail=True,
            realistic=realistic,
        )
    )
    return cases


def write_dataset(cases: list[ProbeCase], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps([c.to_dict() for c in cases], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def load_dataset(path: str | Path) -> list[ProbeCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ProbeCase.from_dict(d) for d in raw]

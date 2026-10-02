"""装配集成测试：把各块**串起来**跑一遍真实的交接。

之前的测试都是单元级的（各自测自己的逻辑）。但整条链路真正的风险在**交接点**：

    CreatePR 在**带改动的工作区**里用 `git diff main` 产出补丁
                    ↓ 交接
    CI 在**干净检出**上用 `git apply` 应用它

这两边对不上（比如补丁带了不该带的文件、上下文行对不上），整条流水线就断了。

这里用真实 git 仓库 + 真实 CI 脚本子进程验证四件事：

  ① 补丁能从一个干净检出上被 `git apply` 成功应用
  ② 应用后 CI 独立重跑验证能过 → dry-run 通过
  ③ 如果 agent **谎报验证通过**（补丁其实是坏的），CI 必须拦下来
  ④ 运维链路能独立跑通，并把结论喂给建单
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.tools import create_default_registry
from mewcode.tools.agent_verify import make_static_verifier
from mewcode.tools.create_pr import CreatePRTool, VerifyResult
from mewcode.tools.ops import MockOpsBackend, register_ops_tools

CI_SCRIPT = Path(__file__).resolve().parent.parent / "ci" / "apply_and_open_pr.py"

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_FILE = (
    "from calc import add\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n"
)


def sh(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check:
        assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个带真实测试的仓库，初始状态是**坏的**（add 写成了减法）。"""
    d = tmp_path / "repo"
    d.mkdir()
    sh(d, "init", "-b", "main")
    sh(d, "config", "user.email", "t@e.com")
    sh(d, "config", "user.name", "T")
    (d / "calc.py").write_text(BUGGY, encoding="utf-8")
    (d / "test_calc.py").write_text(TEST_FILE, encoding="utf-8")
    sh(d, "add", "-A")
    sh(d, "commit", "-m", "init")
    return d


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    return d


def real_verifier(repo: Path):
    """真跑 pytest，用退出码判定 —— 和线上行为一致。"""
    cmd = f'"{sys.executable}" -m pytest test_calc.py -q'

    def _v() -> VerifyResult:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
            cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return VerifyResult(
            ok=r.returncode == 0, command=cmd, exit_code=r.returncode,
            output=(r.stdout or "") + (r.stderr or ""), elapsed=0.0,
        )

    return _v


def make_tool(repo: Path, artifacts: Path, *, verifier, verdict="PASS",
              require_independent=True) -> CreatePRTool:
    return CreatePRTool(
        work_dir=str(repo),
        verify_command="pytest -q",
        artifacts_dir=str(artifacts),
        base_ref="main",
        verifier=verifier,
        verifier_runner=make_static_verifier(f"审查完毕\nVERDICT: {verdict}"),
        require_independent=require_independent,
    )


async def call(tool: CreatePRTool, **kw):
    return await tool.execute(tool.params_model.model_validate(kw))


def run_ci(repo: Path, artifacts: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(CI_SCRIPT), "--repo", str(repo),
         "--artifacts", str(artifacts), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------------------
# ① 交接点：CreatePR 产出的补丁，CI 能不能应用
# ---------------------------------------------------------------------------


class TestPatchHandoff:
    async def test_patch_applies_on_clean_checkout(self, repo, artifacts):
        """★ 核心：agent 在工作区改完 → 出补丁 → **还原工作区** → CI 应用补丁。

        还原这一步是必须的，因为 CI 面对的是一个干净检出，
        不是 agent 那个带着改动的工作区。
        """
        (repo / "calc.py").write_text(FIXED, encoding="utf-8")

        tool = make_tool(repo, artifacts, verifier=real_verifier(repo))
        r = await call(tool, title="fix: 修 add", issue="#1")
        assert not r.is_error, r.output
        assert (artifacts / "changes.patch").exists()

        # 还原成干净检出 —— 模拟 CI 的起点
        sh(repo, "checkout", "--", ".")
        assert (repo / "calc.py").read_text(encoding="utf-8") == BUGGY

        # CI 应用补丁 + 独立重跑
        ci = run_ci(repo, artifacts, "--dry-run",
                    "--verify-cmd", f'"{sys.executable}" -m pytest test_calc.py -q')
        assert ci.returncode == 0, f"CI 失败：\n{ci.stdout}\n{ci.stderr}"
        assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED

    async def test_patch_contains_only_the_intended_change(self, repo, artifacts):
        """补丁里不该混进 __pycache__、日志、临时文件这些噪声。"""
        (repo / "calc.py").write_text(FIXED, encoding="utf-8")
        # 造点噪声文件（未跟踪）
        (repo / "__pycache__").mkdir(exist_ok=True)
        (repo / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\x00")
        (repo / ".mewcode").mkdir(exist_ok=True)
        (repo / ".mewcode" / "pr").mkdir(exist_ok=True)

        tool = make_tool(repo, artifacts, verifier=real_verifier(repo))
        await call(tool, title="t")

        patch = (artifacts / "changes.patch").read_text(encoding="utf-8")
        assert "calc.py" in patch
        assert "__pycache__" not in patch, "补丁里混进了字节码缓存"
        assert "changes.patch" not in patch, "补丁把产物自己也打进去了"


# ---------------------------------------------------------------------------
# ② 谎报验证：agent 说通过了，CI 必须独立抓住
# ---------------------------------------------------------------------------


class TestLyingAgent:
    async def test_ci_catches_broken_patch_despite_pass_verdict(self, repo, artifacts):
        """★ 最关键的一条：让 agent **谎报验证通过**，但补丁其实是坏的。

        构造方式：用假验证器（永远 PASS）产出一个"改坏了"的补丁，
        然后交给 CI —— CI 会**独立重跑**，必须拦下来。

        这正是"不信 agent 自己跑的那次"这个设计要防的事。
        """
        # 把代码改得更坏（仍然可以应用、但测试必挂）
        (repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")

        tool = make_tool(
            repo, artifacts,
            verifier=lambda: VerifyResult(ok=True, command="pytest -q",
                                          exit_code=0, output="装作通过了", elapsed=0.1),
        )
        r = await call(tool, title="fix: 修 add")
        assert not r.is_error, "假验证器应该让它通过（这样才能测 CI 那一层）"

        sh(repo, "checkout", "--", ".")
        # agent 交出的补丁把一个必然失败的实现带过来
        ci = run_ci(repo, artifacts,
                    "--verify-cmd", f'"{sys.executable}" -m pytest test_calc.py -q')

        assert ci.returncode != 0, "CI 没有拦下谎报通过的补丁！"
        report = (artifacts / "FAILURE.md").read_text(encoding="utf-8")
        assert "独立验证" in report
        # 人需要能看出"是 agent 谎报了通过"——所以报告里必须留着 agent 的自述
        assert '"verdict": "PASS"' in report, "报告里没留下 agent 自己声称的结论"
        assert '"exit_code": 0' in report, "报告里没留下 agent 自跑的退出码"
        # 验证者的完整原话也要附上（由 CI 从 verification.md 读取）
        assert "审查完毕" in report, "报告里没附上验证者的推理"

    async def test_ci_blocks_when_agent_produced_no_patch(self, repo, artifacts):
        """agent 什么都没产出时，CI 不该继续往下走。"""
        ci = run_ci(repo, artifacts, "--dry-run")
        assert ci.returncode != 0
        assert not (artifacts / "changes.patch").exists()
        assert (artifacts / "FAILURE.md").exists()


# ---------------------------------------------------------------------------
# ③ 门禁的一条完整拒绝链
# ---------------------------------------------------------------------------


class TestGateChain:
    async def test_command_fail_then_independent_fail_then_pass(self, repo, artifacts):
        """把三层门禁按顺序走一遍：命令验证 → 独立验证 → 产出补丁。"""
        # 阶段 1：代码是坏的 → 命令验证先拦下（且不浪费一次独立验证）
        calls: list[str] = []

        async def counting_runner(_d: str) -> str:
            calls.append("independent")
            return "VERDICT: PASS"

        tool = CreatePRTool(
            work_dir=str(repo), verify_command="pytest -q",
            artifacts_dir=str(artifacts), base_ref="main",
            verifier=real_verifier(repo), verifier_runner=counting_runner,
        )
        r1 = await call(tool, title="t")
        assert r1.is_error and "验证未通过" in r1.output
        assert calls == [], "命令验证都没过，不该跑独立验证（它是贵的）"

        # 阶段 2：代码修好了，但独立验证者判 FAIL
        (repo / "calc.py").write_text(FIXED, encoding="utf-8")
        tool2 = make_tool(repo, artifacts, verifier=real_verifier(repo), verdict="FAIL")
        r2 = await call(tool2, title="t")
        assert r2.is_error and "独立验证" in r2.output
        assert not (artifacts / "changes.patch").exists()

        # 阶段 3：独立验证也过了 → 产出补丁
        tool3 = make_tool(repo, artifacts, verifier=real_verifier(repo), verdict="PASS")
        r3 = await call(tool3, title="fix: 修 add", issue="#1")
        assert not r3.is_error, r3.output
        assert (artifacts / "changes.patch").exists()

        meta = json.loads((artifacts / "pr.json").read_text(encoding="utf-8"))
        assert meta["verify"]["exit_code"] == 0
        assert meta["independent_verify"]["verdict"] == "PASS"

    async def test_require_independent_blocks_when_not_wired(self, repo, artifacts):
        """生产开关：要求独立验证但没接 → 直接拒绝，不许悄悄跳过。"""
        (repo / "calc.py").write_text(FIXED, encoding="utf-8")
        tool = CreatePRTool(
            work_dir=str(repo), verify_command="pytest -q",
            artifacts_dir=str(artifacts), base_ref="main",
            verifier=real_verifier(repo),
            verifier_runner=None,
            require_independent=True,
        )
        r = await call(tool, title="t")
        assert r.is_error and "要求独立验证" in r.output
        assert not (artifacts / "changes.patch").exists()


# ---------------------------------------------------------------------------
# ④ 运维链路独立跑通，并接到建单
# ---------------------------------------------------------------------------


class TestOpsChain:
    async def test_triage_chain_feeds_incident(self):
        """按 SOP 走完整条排查链，最后把证据喂给建单 —— 证明工具之间能对接。"""
        reg = create_default_registry()
        register_ops_tools(reg, MockOpsBackend())

        async def call_ops(name, **args):
            tool = reg.get(name)
            return await tool.execute(tool.params_model.model_validate(args))

        alerts = await call_ops("ListAlerts", severity="critical")
        assert "AL-7781" in alerts.output

        logs = await call_ops("QueryLogs", service="orders-api")
        assert "OrderService.java:142" in logs.output

        metrics = await call_ops("QueryMetrics", metric="http_5xx_rate")
        assert "异常分组" in metrics.output and "pod-3" in metrics.output

        deploys = await call_ops("ListDeploys", service="orders-api", limit=1)
        assert "9b2c1f4" in deploys.output

        # 把上一步的输出**原样**当证据 —— 证明输出格式是可直接引用的
        inc = await call_ops(
            "CreateIncident", service="orders-api",
            title="orders-api 5xx 突增（仅 pod-3）", severity="critical",
            root_cause="v2.14.3 移除了 getTier 里的判空",
            evidence=(
                logs.output.splitlines()[2].strip() + "; "
                + metrics.output.splitlines()[-2].strip() + "; "
                + deploys.output.splitlines()[1].strip()
            ),
        )
        assert not inc.is_error, inc.output
        assert "INC-0001" in inc.output

    async def test_ops_reads_do_not_prompt(self):
        """排查要连查十几次，只读工具在 DEFAULT 模式下必须不弹窗。"""
        from mewcode.permissions import (
            DangerousCommandDetector, PathSandbox, PermissionChecker,
            PermissionMode, RuleEngine,
        )
        from mewcode.tools.ops import OPS_TOOLS

        checker = PermissionChecker(
            detector=DangerousCommandDetector(), sandbox=PathSandbox("."),
            rule_engine=RuleEngine(), mode=PermissionMode.DEFAULT,
        )
        reg = create_default_registry()
        register_ops_tools(reg, MockOpsBackend())

        for cls in OPS_TOOLS:
            if cls.category != "read":
                continue
            d = checker.check(reg.get(cls.name), {})
            assert d.effect == "allow", f"{cls.name} 会弹窗，排查没法做"

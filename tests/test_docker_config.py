"""容器化配置的安全约束测试。

沙箱做错一个地方就等于没做，而**最致命的一处是挂 docker socket**：
挂了它，agent 就能 `docker run --privileged -v /:/host` 拿到宿主 root。
这类错误一旦写进去很难靠 review 发现，所以用测试钉住。

本机沙箱禁具名管道，`bash -n` / `docker build` 都跑不了，
所以这里做的是**静态结构校验** —— 检查"该有的有、不该有的没有"。
真实构建与边界自检要在你的机器上跑（见 README）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKER = Path(__file__).resolve().parent.parent / "docker"
RUN_SCRIPT = DOCKER / "run-agent.sh"
COMPOSE = DOCKER / "compose.test.yml"
DOCKERFILE = DOCKER / "Dockerfile.agent"
CHECK_SCRIPT = DOCKER / "check-sandbox.sh"
DOCKERIGNORE = DOCKER.parent / ".dockerignore"

ALL_DOCKER_FILES = [RUN_SCRIPT, COMPOSE, DOCKERFILE, CHECK_SCRIPT, DOCKERIGNORE]


# ---------------------------------------------------------------------------
# ① 铁律：绝不挂 docker socket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_DOCKER_FILES, ids=lambda p: p.name)
def test_never_mounts_docker_socket(path: Path):
    """★ 最重要的一条。

    挂 `/var/run/docker.sock` 等于把宿主 root 交出去 ——
    agent 可以起一个 `--privileged` 容器把宿主根目录挂进来。
    这是业界最常见的沙箱穿透方式。

    如果将来有人为了"让 agent 自己起测试环境"加上这一行，这条测试会立刻红。
    """
    text = path.read_text(encoding="utf-8")
    # 只检查"挂载"这种用法，注释里提到它是允许的（我们恰恰在注释里警告它）
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if "docker.sock" in code:
            assert not re.search(r"-v\s+\S*docker\.sock", code), (
                f"{path.name} 挂了 docker socket：{line.strip()}"
            )


def test_run_script_explicitly_warns_about_it():
    """光不挂还不够 —— 要写明为什么，否则后人会"顺手补上"。"""
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    assert "docker.sock" in text, "run-agent.sh 里应该有关于这个陷阱的警告"
    assert "绝不" in text or "不要" in text


# ---------------------------------------------------------------------------
# ② 不该给的东西
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RUN_SCRIPT, DOCKERFILE], ids=lambda p: p.name)
def test_no_host_home_or_ssh_mounts(path: Path):
    """宿主 home / .ssh / 云凭证目录一律不挂。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        for forbidden in ("$HOME:", "$HOME/", "~/.ssh", "/root/.ssh", ".aws", ".kube/config"):
            assert forbidden not in code, f"{path.name} 挂了 {forbidden}：{line.strip()}"


def test_dockerfile_does_not_install_docker():
    """镜像里不装 docker —— 装了就等于给了它起容器的能力。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    installed = re.findall(r"apt-get install[^\n]*", text)
    for line in installed:
        assert "docker" not in line, f"镜像装了 docker：{line}"


def test_dockerfile_runs_as_non_root():
    assert re.search(r"^USER\s+(?!root)\w+", DOCKERFILE.read_text(encoding="utf-8"), re.M), (
        "Dockerfile 没有切到非 root 用户"
    )


def test_run_script_drops_capabilities():
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    assert "--cap-drop=ALL" in text
    assert "no-new-privileges" in text


def test_run_script_limits_resources():
    """没有资源限制的话，一个跑飞的 agent 能把宿主机吃干。"""
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    for flag in ("--memory=", "--cpus=", "--pids-limit="):
        assert flag in text, f"缺少资源限制 {flag}"


# ---------------------------------------------------------------------------
# ③ 网络：默认禁出网
# ---------------------------------------------------------------------------


def test_run_script_defaults_to_internal_network():
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    assert "--network" in text
    # 默认（不设 ALLOW_EXTERNAL 时）应该用内网
    assert re.search(r'NET_ARGS=\(--network\s+"\$NETWORK"\)', text), (
        "默认应该接入内部网络，而不是默认 bridge"
    )


def test_compose_network_is_internal():
    """`internal: true` 是"容器出不了网"的机制保证。"""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^\s*internal:\s*true", text, re.M), (
        "compose 的网络没有标 internal: true，容器能自由出网"
    )


def _load_compose() -> dict:
    import yaml

    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_every_service_joins_the_internal_network():
    """★ 结构化校验：**每个服务都必须显式加入那个 internal 网络**。

    这条测试是补上一个真实事故的：

    原来的 compose 声明了 `test-net: {internal: true}`，但**没有任何服务
    引用它**。于是 compose 把三个服务放到了自动创建的
    `<项目名>_default` 网络上 —— 那个网络 `internal=false`。

    实测结果是：`test_docker_config.py` 里那条文本断言
    （"文件里有 internal: true"）**照样通过**，而容器能直连 `1.1.1.1:443`。
    铁律③ 完全失效，但测试是绿的。

    教训和"负向断言空过"一样：**断言必须落在效果上，而不是落在声明上。**
    这里至少能落到结构上 —— 声明了却没人用的网络，等同于没声明。
    """
    cfg = _load_compose()
    declared = cfg.get("networks") or {}
    internal_nets = {
        name for name, spec in declared.items() if isinstance(spec, dict)
        and spec.get("internal") is True
    }
    assert internal_nets, "compose 里没有声明 internal 网络"

    services = cfg.get("services") or {}
    assert services, "compose 里没有服务"

    for name, svc in services.items():
        joined = svc.get("networks") or []
        if isinstance(joined, dict):
            joined = list(joined)
        assert internal_nets & set(joined), (
            f"服务 {name!r} 没有加入 internal 网络 {sorted(internal_nets)} "
            f"（实际加入的是 {joined!r}）—— 它会被放到自动创建的 "
            f"`<项目名>_default` 网络上，那个网络不是 internal 的，"
            f"容器能自由出网，铁律③ 失效"
        )


def test_internal_network_name_matches_run_script_default():
    """compose 建出来的网络全名必须和 run-agent.sh 的默认值对得上。

    否则 `run-agent.sh` 会直接报 `network ... not found` —— 这是实测踩到的
    另一个后果：脚本默认指向一个名字对不上的网络。
    """
    cfg = _load_compose()
    project = cfg.get("name")
    assert project, "compose 没有显式 name，网络全名会随目录名变"

    internal = [
        n for n, s in (cfg.get("networks") or {}).items()
        if isinstance(s, dict) and s.get("internal") is True
    ]
    assert len(internal) == 1, f"预期恰好一个 internal 网络，实得 {internal}"

    expected = f"{project}_{internal[0]}"
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'NETWORK="\$\{MEWCODE_NET:-([^}]+)\}"', text)
    assert m, "run-agent.sh 里找不到 NETWORK 的默认值"
    assert m.group(1) == expected, (
        f"run-agent.sh 默认网络 {m.group(1)!r} 与 compose 实际创建的网络 "
        f"{expected!r} 不一致 —— 脚本会报 network not found"
    )


def test_check_script_does_not_assert_on_distro_paths():
    """沙箱自检里不能出现"在任何镜像里都为真/为假"的断言。

    踩过的例子：`test -d /root` —— `/root` 是基础镜像自带的目录，
    任何 Debian/Ubuntu 容器里都为真，所以"期望 deny"的那条**永远 FAIL**，
    而 FAIL 的原因跟"宿主 home 有没有被挂进来"毫无关系。
    一个永远失败的检查会让人开始忽略检查结果，比没有检查更糟。
    """
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(
        line.split("#", 1)[0] for line in text.splitlines()
    )
    assert not re.search(r"check\s+\"[^\"]*home[^\"]*\"\s+deny\s+test\s+-d\s+/root", code), (
        "check-sandbox.sh 又写回了 `test -d /root` 这种恒为真的断言"
    )


def test_compose_does_not_publish_host_ports():
    """不该把测试服务的端口暴露到宿主机 —— 那会给宿主上的其它程序开个口子。"""
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        assert not re.match(r"\s*ports:", code), f"compose 暴露了宿主端口：{line.strip()}"


# ---------------------------------------------------------------------------
# ④ 脚本自身结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RUN_SCRIPT, CHECK_SCRIPT], ids=lambda p: p.name)
def test_shell_scripts_are_strict(path: Path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!"), f"{path.name} 缺 shebang"
    assert "set -euo pipefail" in text or "set -uo pipefail" in text, (
        f"{path.name} 没开严格模式，一个失败命令会被忽略"
    )


def test_check_script_covers_the_key_boundaries():
    """边界自检脚本必须覆盖这几项，否则它给了虚假的安全感。"""
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    for needle in (
        "id -u",                    # 不是 root
        "command -v docker",        # 没有 docker
        "docker.sock",              # 没有 socket
        ".ssh",                     # 拿不到宿主 ssh
        "/dev/tcp",                 # 出不了网
        "exit 1",                   # 有失败就非 0 退出
    ):
        assert needle in text, f"check-sandbox.sh 没有检查：{needle}"


def test_run_script_mounts_only_work_and_artifacts():
    """只挂两个目录：代码（读写）和产物出口（读写）。

    必须先剥注释 —— run-agent.sh 的注释里恰好写了一句
    ``docker run --privileged -v /:/host``，那是**反面教材**，不是真挂载。
    """
    text = RUN_SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    mounts = re.findall(r"-v\s+\S+", code)
    assert len(mounts) == 2, f"挂载点数量不对（应为 2）：{mounts}"
    assert any("/work" in m for m in mounts)
    assert any("/artifacts" in m for m in mounts)


# ---------------------------------------------------------------------------
# ⑤ .dockerignore：别把会话内容烘进镜像
# ---------------------------------------------------------------------------


def test_dockerignore_excludes_local_state():
    """`.mewcode/` 里有真实会话内容 —— 绝不能进镜像。"""
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for pattern in (".mewcode/", "__pycache__/", ".git", "*.local.yaml", ".env"):
        assert pattern in text, f".dockerignore 没有排除 {pattern}"


def test_dockerignore_excludes_credentials():
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "*.local.yaml" in text, "本地配置（可能含凭据）会进镜像"
    assert ".env" in text

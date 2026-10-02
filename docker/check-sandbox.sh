#!/usr/bin/env bash

# 沙箱边界自检 —— **在容器里跑**，验证"外面够不着"这件事真的成立。
#
#   docker run --rm --network <net> -v ... mewcode-agent sh /app/mewcode/docker/check-sandbox.sh
#
# 全部应为 PASS。任何一条 FAIL 都说明沙箱没做对。
set -uo pipefail

pass=0
fail=0

check() {  # check <描述> <期望: deny|allow> <命令...>
    local desc="$1" expect="$2"; shift 2
    if "$@" >/dev/null 2>&1; then got=allow; else got=deny; fi
    if [[ "$got" == "$expect" ]]; then
        printf '  ✅ %-46s %s\n' "$desc" "$got"
        pass=$((pass + 1))
    else
        printf '  ❌ %-46s 期望 %s，实得 %s\n' "$desc" "$expect" "$got"
        fail=$((fail + 1))
    fi
}

echo "沙箱边界自检"
echo "──────────────────────────────────────────────────────────"

# ── 文件系统边界 ────────────────────────────────────────────
#
# ⚠️ 这里曾经写的是 `check "宿主 home 不存在" deny test -d /root` —— 那是个
#    **永远不可能通过**的断言：`/root` 是基础镜像自带的目录，任何 Debian/Ubuntu
#    容器里 `test -d /root` 都为真，所以它只会一直报 FAIL，而 FAIL 的原因
#    跟"宿主 home 有没有被挂进来"毫无关系。
#
#    真正该断言的是：**当前用户读不到那个目录**。容器自己的 /root 存在无害，
#    只要 agent（非 root）打不开它就行。宿主 home 如果被误挂进来，
#    会出现在宿主上的原路径（/home/<user> 之类），下面那几条 + $HOME 检查覆盖。
check "/root 不可读（当前非 root）"  deny  test -r /root
check "宿主 ssh 目录不存在"      deny  test -d "$HOME/.ssh"
check "宿主 ~/.aws 不存在"       deny  test -d "$HOME/.aws"
check "宿主 ~/.mewcode 不存在"   deny  test -d "$HOME/.mewcode"
check "HOME 下没有 ssh 私钥"     deny  bash -c 'ls -A "$HOME/.ssh" 2>/dev/null | grep -q .'
check "代码目录可写（应该有）"     allow test -w /work
check "产物出口可写（应该有）"     allow test -w /artifacts

# ── 权限边界 ────────────────────────────────────────────────
check "不是 root"               deny  test "$(id -u)" = "0"
check "没有 docker 命令"         deny  command -v docker
check "没有 docker socket"       deny  test -S /var/run/docker.sock
check "没有 kubectl"            deny  command -v kubectl
check "没有 ssh 客户端"          deny  command -v ssh

# ── 网络边界 ────────────────────────────────────────────────
check "连不上公网（应该连不上）"   deny  bash -c 'timeout 3 bash -c "> /dev/tcp/1.1.1.1/443"'
check "DNS 解析不了外网域名"      deny  bash -c 'getent hosts github.com >/dev/null'

echo "──────────────────────────────────────────────────────────"
echo "  PASS $pass / FAIL $fail"

if [[ "$fail" -gt 0 ]]; then
    echo "  ⚠️  存在边界问题，先修沙箱再跑正式任务"
    exit 1
fi
echo "  ✅ 沙箱边界成立"

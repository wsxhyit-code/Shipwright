#!/usr/bin/env bash

# 启动 agent 容器。这个脚本就是「沙箱」的实体。
#
#   ./docker/run-agent.sh <仓库目录> <产物目录> "<任务描述>"
#
# 例：
#   ./docker/run-agent.sh ~/myrepo /tmp/artifacts "修 issue #142 的导出乱码"
#
# ────────────────────────────────────────────────────────────────
# 三条铁律（每一条都对应一个真实的沙箱穿透）
# ────────────────────────────────────────────────────────────────
#
#  ① **绝不挂 docker.sock**
#     挂了它，agent 就能 `docker run --privileged -v /:/host` 拿到宿主 root。
#     这是业界最常见的沙箱穿透。所以本脚本里连提都不提它。
#
#  ② **绝不挂宿主 home / ssh / 云凭证**
#     只挂两个目录：代码（读写）和产物出口（读写）。其余一概不给。
#
#  ③ **默认禁出网**
#     容器只能连测试网络里的服务。需要 pip 装包时用 --allow-external 临时放行，
#     装完再跑正式任务。否则 agent 既能把代码发出去，也能拉任意代码进来。
#
set -euo pipefail

REPO_DIR="${1:?用法: run-agent.sh <仓库目录> <产物目录> [任务描述]}"
ARTIFACTS_DIR="${2:?用法: run-agent.sh <仓库目录> <产物目录> [任务描述]}"
TASK="${3:-}"
IMAGE="${MEWCODE_IMAGE:-mewcode-agent:latest}"
NETWORK="${MEWCODE_NET:-mewcode-agent-env_test-net}"
ALLOW_EXTERNAL="${ALLOW_EXTERNAL:-0}"

REPO_DIR="$(cd "$REPO_DIR" && pwd)"
mkdir -p "$ARTIFACTS_DIR"
ARTIFACTS_DIR="$(cd "$ARTIFACTS_DIR" && pwd)"

if [[ "$ALLOW_EXTERNAL" == "1" ]]; then
    echo "⚠️  --allow-external：本次放行出网（仅应用于安装依赖，不要用于正式任务）" >&2
    NET_ARGS=(--network bridge)
else
    NET_ARGS=(--network "$NETWORK")
fi

echo "→ 仓库     : $REPO_DIR"
echo "→ 产物出口 : $ARTIFACTS_DIR"
echo "→ 网络     : ${NET_ARGS[1]}"
echo

exec docker run --rm \
    "${NET_ARGS[@]}" \
    \
    `# ① 只挂这两个目录` \
    -v "$REPO_DIR:/work:rw" \
    -v "$ARTIFACTS_DIR:/artifacts:rw" \
    \
    `# ② 环境变量：连接串由平台注入，agent 不需要也不允许自己拼` \
    -e DATABASE_URL="postgresql://app:app@db:5432/testdb" \
    -e REDIS_URL="redis://redis:6379" \
    -e MOCK_HTTP="http://mock-http:8080" \
    -e MEWCODE_VERIFY_CMD="${MEWCODE_VERIFY_CMD:-python -m pytest tests/ -q}" \
    -e MEWCODE_BASE_REF="${MEWCODE_BASE_REF:-main}" \
    \
    `# ③ 资源限制：防止 agent 把宿主机吃干` \
    --memory=2g \
    --cpus=2 \
    --pids-limit=512 \
    \
    `# ④ 只读根文件系统 + 一个可写的 tmp` \
    --read-only \
    --tmpfs /tmp:rw,size=512m \
    \
    `# ⑤ 降权：即使镜像被改坏也不给新特权` \
    --security-opt=no-new-privileges \
    --cap-drop=ALL \
    \
    -w /work \
    "$IMAGE" \
    -p "$TASK"

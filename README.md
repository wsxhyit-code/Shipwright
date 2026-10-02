# Shipwright

**能自己把活干完的 coding agent**：读代码 → 定位问题 → 改代码 → 跑测试 → 自己验证 → 推 `agent/*` 分支 → 开 PR。

跟"让模型自由发挥"的区别在于：**边界是机械钉死的，不是靠提示词祈祷的。**

---

## 三道机械护栏

### ① 绝不碰基线分支

改动只落在 `agent/<标题>-<sha>` 上。这不是"我们记得别推 `main`" ——
`delivery.guard_branch` **只放行 `agent/` 前缀**，所以调用方想推基线分支也推不了，
因为那条路走不通。

```python
delivery.guard_branch("main", base="main", remote="origin")
# DeliveryError: 拒绝推送受保护的分支名 'main'
delivery.guard_branch("feature/x", base="main", remote="origin")
# DeliveryError: 拒绝推送 'feature/x'：分支名必须以 'agent/' 开头
```

PR 的 `base` 是基线、`head` 是 agent 分支，永远不可能自己合自己。

### ② 自己说"过了"不算数

两层门禁，**顺序硬编码**：

```
命令验证（退出码）  →  独立验证者（另一个 agent 审）  →  提交  →  推送  →  开 PR
    便宜，先跑              贵，后跑              ↑
                                    验证没过就走不到这里
```

独立验证者的"独立性"由机械保证，不靠提示词：

| 维度 | 保证方式 |
|---|---|
| 上下文独立 | 全新 `ConversationManager()`，看不到实现对话 |
| 工具只读 | 注册表里只放 `ReadFile` / `Grep` / `Glob`，**`WriteFile` / `Bash` / `CreatePR` 根本不存在** |
| 权限兜底 | 再加 `PermissionMode.PLAN`，即使注册表被绕过也写不了 |
| 结论必须显式 | 必须输出 `VERDICT: PASS` / `VERDICT: FAIL` |
| **没结论 = 不通过** | 解析不到就按 FAIL（fail-closed） |

它拿到的提示词要求的是**找茬**而不是**确认** —— 只要求"确认对不对"时，模型倾向于给你想要的答案。

### ③ CI 会把它跑过的验证再跑一遍

实现者可能跑错命令、跳过测试、或改完测试忘了重跑。收尾脚本 `ci/apply_and_open_pr.py`
里**没有一行 AI**，只有：`git apply --check` → 独立重跑 → 推送 → 开 PR。

重跑失败就写 `FAILURE.md`，并把 agent 自己的验证记录一起附上 —— 这样人能看出是
**「agent 谎报通过」**还是**「代码真的坏了」**。

---

## 快速开始

```bash
git clone https://github.com/<你>/<仓库>.git
cd <仓库名>
pip install -e .

cp .mewcode/config.yaml.example .mewcode/config.yaml   # 填你的 provider 与 api_key
python -m mewcode
```

依赖（Python ≥ 3.11）：

```
anthropic>=0.42.0   httpx>=0.27.0     mcp>=1.12.0      openai>=1.60.0
pydantic>=2.0       pyyaml>=6.0       rich>=13.0       textual>=2.1.0
```

开发和测试额外需要 `pytest` + `pytest-asyncio`（`pip install -e ".[dev]"`）。

> **关于目录名**：这个仓库用的是"源码根 = 包根"的扁平布局
> （`__init__.py` 就在仓库根，源码里一律写 `from mewcode.tools import ...`），
> 靠 `pyproject.toml` 里的 `package-dir = {"mewcode": "."}` 映射成可安装的包。
>
> 所以：**`pip install -e .` 之后随便叫什么目录名都能用**。
> 但如果你不安装、直接 `python -m mewcode`，那就要求目录名恰好是 `mewcode`
> （因为源码运行靠的是父目录在 `sys.path` 上）。
>
> ⚠️ 打包配置里**刻意没有用 `packages.find`** —— 它会在仓库根扫出
> `agents` / `tools` 这种顶层包而不是 `mewcode.*`，打出来的 wheel 里文件直接
> 躺在 site-packages 根目录，`import mewcode` 根本不存在。包清单是显式列出的。

---

## 配置：怎么把这些能力开起来

内置只有 6 个工具（`ReadFile` / `WriteFile` / `EditFile` / `Bash` / `Glob` / `Grep`）。
运维工具和 `CreatePR` 由配置装配 —— **不写这段，agent 就够不着它们**：

```yaml
toolset:
  # ① 让 agent 具备「提 PR」这个动作
  create_pr:
    enabled: true
    verify_command: "python -m pytest tests/ -q"   # 必填，没有它启动就报错
    artifacts_dir: ".mewcode/pr"
    base_ref: "main"
    require_independent: true                      # 必须配独立验证者，否则拒绝
    timeout: 900

    # 交付模式 —— agent 自己走到哪一步
    mode: pr            # patch（只出补丁）| push（推 agent/* 分支）| pr（再开 PR）
    remote: origin
    api_base: ""        # 留空 = https://api.github.com；自建实例填 https://<host>/api/v3

  # ② 接运维数据源（不写就是没接，调用会明确报错）
  ops:
    - kind: loki
      base_url: "https://loki.internal"
      token_env: "LOKI_READONLY_TOKEN"             # 从环境变量读，配置里不落密钥
    - kind: prometheus
      base_url: "https://prom.internal"
    - kind: alertmanager
      base_url: "https://am.internal"
      token_env: "AM_TOKEN"
```

### 三种交付模式

| mode | agent 走到哪一步 | 需要什么 | 适用 |
|---|---|---|---|
| **`patch`**（默认） | 只产出 `changes.patch` / `pr.json` / `verification.md`，**不推送** | 无 | 容器模式（agent 连不上远端，由 CI 收尾） |
| **`push`** | 加上：自己提交 → 推 `agent/*` 分支 | 远端 + 推送凭据 | 想让人或 CI 来开 PR |
| **`pr`** | 加上：直接开 PR | 上面 + `gh` 或 `GITHUB_TOKEN` | 完整自治 |

**三种模式的护栏完全一样。** `mode: pr` 但本机既没有 `gh` 也没有 token 时，
配置校验会在**启动时**报错 —— 不这么做的话，agent 会改完代码、两层验证都通过、
分支都推上去之后才发现开不了 PR（"跑到最后一米才失败"是最浪费的失败方式）。

### 几条刻意做"严"的校验

| 规则 | 为什么 |
|---|---|
| `enabled: true` 却没写 `verify_command` → 启动报错 | 没有验证命令的 CreatePR 等于把铁律拆了，而它看起来还在工作。**静默降级比启动失败危险得多** |
| `token_env` 读不到 → 启动报错 | 否则会静默发出无认证请求，拿到 401 —— 而 401 在排查时容易被误读成"服务端权限配错了" |
| 没接的能力调用时抛错，**不返回空** | 返回空会让模型把"没接入"读成"没有异常"，然后非常自信地得出错误结论 |

---

## 完整流程

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【准备】确定性，无 AI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  docker compose -f docker/compose.test.yml up -d --wait
  docker build -f docker/Dockerfile.agent -t mewcode-agent .

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【AI 内核】唯一需要 LLM 的一段
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Grep / ReadFile 定位问题
  EditFile 改代码（post_tool_use hook 自动 ruff format）
  Bash: pytest -q
      ↓
  调用 CreatePR
    ├─ ① 命令验证：退出码必须 0
    ├─ ② 独立验证：另一个 agent 审（新上下文 + 只读工具集 + plan 模式）
    └─ ③ 按 mode 交付：出补丁 / 推 agent/* / 开 PR
       ★ 全程没有 git push 到基线分支

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【收尾】确定性，无 AI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python ci/apply_and_open_pr.py --repo . --artifacts .mewcode/pr \
      --verify-cmd "pytest -q" --base main
    ├─ git apply --check → git apply      （脏补丁在这里就被拦下）
    ├─ 独立重跑验证                        （不信 agent 自己跑的那次）
    ├─ git checkout -B agent/<slug>-<sha>  （绝不推基线分支）
    ├─ git push -u origin <branch>
    └─ 开 PR（gh 或 REST API）
```

---

## 容器沙箱：靠"不给"，不是靠"禁止"

| # | 铁律 | 不做会怎样 |
|---|---|---|
| **①** | **绝不挂 `docker.sock`** | agent 能 `docker run --privileged -v /:/host` 拿到宿主 root。**这是业界最常见的沙箱穿透** |
| **②** | **绝不挂宿主 `$HOME` / `.ssh` / 云凭证** | 只挂 `/work`（代码）和 `/artifacts`（产物） |
| **③** | **默认禁出网** | 否则 agent 能把代码发出去、也能拉任意代码进来 |

实测（`docker/check-sandbox.sh`，14/14 通过）：

```
  ✅ /root 不可读（当前非 root）      ✅ 不是 root
  ✅ 宿主 ssh 目录不存在               ✅ 没有 docker 命令
  ✅ 宿主 ~/.aws 不存在                ✅ 没有 docker socket
  ✅ 宿主 ~/.mewcode 不存在            ✅ 没有 kubectl
  ✅ HOME 下没有 ssh 私钥              ✅ 没有 ssh 客户端
  ✅ 代码目录可写 / 产物出口可写        ✅ 连不上公网 / DNS 解析不了外网
```

> **容器模式才是"agent 连不上远端"的物理保证**：没有网络出口、没有 git 凭据、
> 没有宿主 home。本地 TUI 模式没有这层隔离，所以护栏必须在代码里（见开头那三条）。

### 「起测试环境」分三层，只有两层该给 agent

| 层 | 例子 | 谁做 | 为什么 |
|---|---|---|---|
| **依赖服务** | PostgreSQL / Redis / mock | **平台预先起好** | 起容器要 docker 权限 → 给了就破沙箱 |
| **项目依赖** | `pip install -r requirements.txt` | ✅ agent | 每个项目不同，且随代码变 |
| **测试数据** | `alembic upgrade head` | ✅ agent | 跟着数据模型走 |

**平台造环境，agent 用环境**：连接串通过环境变量注入，agent 只能"连"不能"起"。

---

## 除了改代码，它还能做运维

接了 Loki / Prometheus / Alertmanager 之后，它能走完一条完整链路：

```
告警说 5xx 涨了  →  日志聚类指向 OrderService.java:142 的 NPE
                 →  指标显示**只有 pod-3 异常**
                 →  部署记录显示 pod-3 在 12 分钟前更新过
                 →  去看这次发布的代码改动（用 coding 工具）
                 →  改完、自己验证过、提 PR
```

**这是普通运维 AI 做不到的** —— 它只能告诉你"pod-3 有问题，建议重启"。

日志聚类在客户端做：1,204 条日志聚成 1 类，只给「签名 / 条数 / 时间范围 / 样例 3 行」。
不聚类的话就是把 240KB 塞进上下文，正好踩上"工具结果超限 → 落盘 → 只给 2KB 预览"那个坑。

---

## 项目结构

```
__main__.py  app.py            入口（TUI 与 -p 两条装配路径）
agent.py                        ReAct 主循环
client.py                       Anthropic / OpenAI / OpenAI-compat 三个客户端
context/manager.py              两层压缩（大工具结果落盘 + 摘要）

tools/                          内置工具 + 扩展工具
  create_pr.py                  CreatePR 门禁（三种交付模式）
  agent_verify.py               独立验证子 agent
  ops/                          9 个运维工具
    backends/                   Loki / Prometheus / Alertmanager 真实实现 + 工厂
delivery.py                     提交 / 推 agent/* / 开 PR（agent 与 CI 共用）
toolset.py                      配置 → 工具装配
permissions/                    Layer 0-5 权限检查 + PathSandbox
hooks/                          事件钩子
skills/  memory/  sessions/     Skill / 记忆 / 会话（JSONL 持久化，无数据库）
mcpclient/                      MCP 客户端

docker/                         容器沙箱（三条铁律）
ci/                             确定性收尾流水线（无 AI）
docs/devops-agent/              说明 + 可跑 demo
tests/                          357 条（默认）/ 378 条（含真实推送）+ 7 条真实 LLM
```

### 跑测试

```bash
python -m pytest              # 357 passed, 22 skipped（默认）
python -m pytest -m llm       # 7 条真实 LLM 测试（要 API key，约 8 分钟）
```

---

## 验证状态（诚实说明）

**已端到端实测：**

| 项 | 怎么验的 |
|---|---|
| ★ **独立验证者能抓到埋进去的 bug** | **真实 LLM**（`pytest -m llm`）：植入"缺判空"的 bug → 判 FAIL，且**理由里点到了 `None` / 判空 / `KeyError`** —— 证明它真读懂了代码，不是碰巧 |
| ★ **验证者有区分能力** | 反向用例：实现正确时判 PASS —— 一个永远 FAIL 的验证者和不存在一样没用，还会堵死流程 |
| 验证者物理上改不了文件 | 只读注册表 + `plan` 模式，真实模型也改不动 |
| 验证门禁拦得住没改好的代码 | `demo_full_pipeline.py`：代码没修 → 被拒 → **不产出补丁** |
| 补丁应用 / 独立重跑 / 推送 / 开 PR | 同上的 demo 与 `tests/test_autonomous_delivery.py`，exit 0 |
| 真实 `git push` | 推到真实 bare 远端，`agent/*` 出现、`main` 未被碰 |
| 开 PR 的 HTTP 请求 | `--api-base` 指向本地假 GitHub API，真发 POST，路径/认证头/payload 全验证 |
| 沙箱三条铁律 | 真实运行标志 + 真实网络：容器能连内网服务、连不上公网；自检 14/14 |
| ★ **对着真 GitHub 开过 PR** | 真跑一次完整流程：补丁 → `git apply` → 独立重跑 → 建 `agent/*` 分支 → **真 push** → **真调 `api.github.com` 开 PR**。核实结果：PR 状态 `open`、`head=agent/...`、`base=main`，且**基线 `main` 仍是最初那一个 commit，一动没动** |
| **仓库可安装** | 构建 wheel → 解开 → `import mewcode` + 关键子模块 + `styles.tcss` + 内置 skill 全部正常 |

**未验证 / 有边界：**

| 项 | 说明 |
|---|---|
| **容器镜像实际构建** | 构建容器的出网环境受限（实测 `deb.debian.org` / 阿里云镜像都不可达），镜像未建出来。运行时边界已单独验证 |
| **真实 Loki / Prometheus 实例** | 单测用 `httpx.MockTransport`（验证请求构造与解析），demo 真起 HTTP 服务 —— 但那是本地假实现。**接生产前必须对真实例验一次字段名**（各家 `stream` 标签、`severity` 取值可能不一样） |
| **部署 / 工单后端** | 没有通用标准，需要自己写适配器接 ArgoCD / Jenkins / Jira。接口就 8 个方法 |

### 想真验一次"对着真 GitHub 开 PR"

```bash
export GITHUB_TOKEN=ghp_xxx          # 需要 repo 权限
python ci/apply_and_open_pr.py --repo . --artifacts .mewcode/pr \
    --verify-cmd "python -m pytest tests/ -q" --base main
```

---

## 一次设计更正：为什么允许 agent 自己推送

最早的设计是「AI 只产出补丁、**永不推送**」，靠一个 hook 拦 `git push`。
实测那个 hook：

| 命令 | 结果 |
|---|---|
| `git push` / `bash -c "git push"` / `/usr/bin/git push` / `cd x && git push` | 拦住 |
| **`git -C . push`** | **放行 ← 绕过** |
| **`git -c core.pager=cat push`** | **放行 ← 绕过** |
| **`python -c "subprocess.run(['git','push'])"`** | **放行 ← 绕过** |
| **`git\ push`** / **`$(which git) push`** | **放行 ← 绕过** |

5 / 9 种写法能绕过，而 `git -C <目录> push` 恰恰是 agent 最常用的写法 ——
所以"AI 永不推送"实际是一个**可绕过的正则**，不是保证。

与其用它假装拦住，不如明确允许推送，把真正承重的三条守住（就是开头那三条）。
**把"AI 只能产出补丁"当成铁律，是把实现手段当成了目的。**

---

## License

（待补）

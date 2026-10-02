# 云端代码运维 Agent：三层实现

「AI 自己验证通过了，才允许提 PR」——而且它的能力被物理限制在"产出补丁"这一步。

```
   ① 独立验证门禁     tools/create_pr.py + tools/agent_verify.py
   ② 容器化沙箱       docker/
   ③ CI 确定性流水线   ci/
   ④ 接入层（新增）    toolset.py + config.yaml 的 toolset 段
```

---

## 先看这里：怎么把这些能力开起来

**这一段是之前真正缺的东西。** 上面三块代码写完了、测试也全过，
但 `app.py` 只注册 6 个内置工具（ReadFile / WriteFile / EditFile / Bash / Glob / Grep），
`CreatePR` 和运维工具**从来没被注册给任何一次运行的 agent**。

也就是说：`python -m mewcode` 起来的 agent，**根本够不着 `CreatePR`**。
文档里那条"agent 自己验证通过才提 PR"的流程，用当时的入口是跑不出来的 ——
属于"接了但没通电"。

现在由配置驱动装配。在 `.mewcode/config.yaml` 里加一段即可：

```yaml
toolset:
  # ① 让 agent 具备「提 PR」这个动作
  create_pr:
    enabled: true
    verify_command: "python -m pytest tests/ -q"   # 必填，没有它直接报错
    artifacts_dir: ".mewcode/pr"                   # 补丁 / pr.json / verification.md 的出口
    base_ref: "main"                               # 补丁相对哪个基线算 diff
    require_independent: true                      # 必须配独立验证者，否则拒绝
    timeout: 900

    # 交付模式 —— agent 自己走到哪一步
    mode: pr            # patch（只出补丁）| push（推 agent/* 分支）| pr（再开 PR）
    remote: origin
    api_base: ""        # 留空 = https://api.github.com；自建实例填 https://<host>/api/v3

  # ② 接运维数据源（不写就是没接，调用会明确报错）
  ops:
    - kind: alertmanager
      base_url: "https://am.internal"
      token_env: "AM_TOKEN"                        # 从环境变量读，配置里不落密钥
    - kind: loki
      base_url: "https://loki.internal"
      token_env: "LOKI_READONLY_TOKEN"
    - kind: prometheus
      base_url: "https://prom.internal"
    # 部署 / 工单没有通用标准，需要你自己写适配器；不接就保持不写
```

### 三种交付模式

| mode | agent 走到哪一步 | 需要什么 | 适用 |
|---|---|---|---|
| **`patch`**（默认） | 只产出 `changes.patch` / `pr.json` / `verification.md`，**不推送** | 无 | 容器模式（agent 连不上远端，由 CI 收尾） |
| **`push`** | 加上：自己提交 → 推 `agent/*` 分支 | 远端 + 推送凭据 | 想让人或 CI 来开 PR |
| **`pr`** | 加上：直接开 PR | 上面 + `gh` 或 `GITHUB_TOKEN` | 完整自治：写代码 → 跑测试 → 推 → 开 PR |

**三种模式的护栏完全一样**：绝不推基线分支、绝不合并、验证必须先于推送。

`mode: pr` 但本机既没有 `gh` 也没有 token 时，配置校验会在**启动时**报错。
不这么做的话，agent 会改完代码、两层验证都通过、分支都推上去之后才发现开不了 PR ——
"跑到最后一米才失败"是最浪费的失败方式。

### ⚠️ 为什么允许 agent 自己推送（一次设计更正）

原来这里是「AI 只产出补丁，**永不推送**」，靠一个 hook 拦 `git push`。
实测那个 hook：

| 命令 | 结果 |
|---|---|
| `git push` / `git push -u origin main` / `git  push` | 拦住 |
| `bash -c "git push"` / `cd x && git push` / `/usr/bin/git push` | 拦住 |
| **`git -C . push`** | **放行 ← 绕过** |
| **`git -c core.pager=cat push`** | **放行 ← 绕过** |
| **`python -c "subprocess.run(['git','push'])"`** | **放行 ← 绕过** |
| **`git\ push`**（反斜杠转义空格） | **放行 ← 绕过** |
| **`$(which git) push`** | **放行 ← 绕过** |

5 / 9 种写法能绕过。而 `git -C <目录> push` 恰恰是 agent 最常用的写法 ——
所以"AI 永不推送"实际是一个**可绕过的正则**，不是保证。

真正承重的三条其实和"谁推送"无关：

1. **绝不推基线分支** —— 由 `delivery.guard_branch` **结构性**保证：
   分支名不以 `agent/` 开头直接拒绝，所以这条路**推不到** `main`。
   不是"我们记得别推"，是走不通。
2. **绝不合并** —— 只开 PR。PR 是**提议**，合并由人点。
3. **验证先于推送** —— 顺序在 `CreatePRTool.execute` 里硬编码：
   命令验证 → 独立验证 → 提交 → 推送 → 开 PR。
   `tests/test_autonomous_delivery.py` 里有一条专门钉这个顺序
   （它让验证者在被调用时检查本地**还没有任何新提交**）。

> 容器模式下另有一层物理保证：agent 连不上远端（无网络出口 + 无凭据 + 无宿主 home）。
> 本地 TUI 模式没有这层隔离，所以护栏必须在代码里 —— 而不是靠 hook。

装配结果（`tests/test_toolset_wiring.py` 里逐条断言过）：

| 配置 | 装配后新增的工具 |
|---|---|
| `create_pr.enabled: true` | `CreatePR`（category=`command`，独立验证者自动接上，交付模式由 `mode` 决定） |
| `ops:` 三个后端 | `ListAlerts` `GetAlert` `QueryLogs` `GetLogSample` `QueryMetrics` `ListDeploys` `GetServiceHealth` `BuildTimeline` `CreateIncident` |
| 两段都不写 | **和以前完全一样**（6 个内置工具），不影响任何现有用法 |

四条刻意做"严"的地方：

| 规则 | 为什么 |
|---|---|
| `enabled: true` 却没写 `verify_command` → **启动就报错** | 没有验证命令的 CreatePR 等于把那条铁律拆了，而它看起来还在工作。静默降级比启动失败危险得多 |
| `token_env` 声明的环境变量读不到 → **启动就报错** | 否则会静默发出一个无认证请求，拿到 401 —— 而 401 在排查时很容易被误读成"服务端权限配错了" |
| `kind` / `capability` 拼错、同一能力配两个后端 → 报错 | 这份配置只在**真出事时**才被用到，那时才发现"配置根本没生效"，代价是一次故障排查 |
| 没接的能力调用时抛 `OpsCapabilityMissing` | 返回空会让模型把"没接入"读成"没有异常"，然后非常自信地得出错误结论 |

`describe()` 会把能力清单交给模型（`✓ 告警 / ✓ 日志 / ✗ 部署`），
让它**在排查开始前**就知道哪些数据源不可用。

> `docker/run-agent.sh` 走的是 `mewcode -p`，那是**另一条**装配路径
> （`__main__._run_prompt` 里自己建 registry）。两条路径都接了 ——
> 只接 TUI 的话，容器里的 agent 依然够不着 `CreatePR`。

---

## 一、完整运行流程

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【准备】确定性，无 AI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  docker compose -f docker/compose.test.yml up -d --wait
      └─ 起 testdb / redis / mock-http 到私有网络 test-net
      └─ 网络标了 internal: true → 容器连不出去

  docker build -f docker/Dockerfile.agent -t mewcode-agent .

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【AI 内核】docker/run-agent.sh —— 唯一需要 LLM 的一段
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  docker run --rm \
      --network test-net \              ← 只能连测试网络
      -v repo:/work:rw \
      -v artifacts:/artifacts:rw \      ← 只挂这两个
      --read-only --tmpfs /tmp \
      --cap-drop=ALL --security-opt=no-new-privileges \
      --memory=2g --cpus=2 \
      mewcode-agent -p "<任务>"
      │
      │  （注意：没有 -v /var/run/docker.sock，没有挂 $HOME）
      │
      └─ agent 在里面：
           读 MEWCODE.md（规范 + 环境边界自动注入）
           Grep / ReadFile 定位问题
           EditFile 改代码（post_tool_use hook 自动 ruff format）
           Bash: pip install -r requirements.txt
           Bash: alembic upgrade head
           Bash: pytest -q
           ↓
           调用 CreatePR
             ├─ ① 命令验证：退出码必须 0        ← 便宜，先跑
             ├─ ② 独立验证：另一个 agent 审     ← 贵，后跑
             │      全新上下文 + 只读工具集
             │      必须显式输出 VERDICT: PASS
             │      没给结论 = 不通过（fail-closed）
             └─ 产出 /artifacts/changes.patch + pr.json + verification.md
                ★ 全程没有 git push

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【收尾】确定性，无 AI —— agent 已退场
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python ci/apply_and_open_pr.py --repo . --artifacts artifacts \
      --verify-cmd "pytest -q" --base main
      │
      ├─ git apply --check  →  git apply        （脏补丁在这里就被拦下）
      ├─ ★ 独立重跑验证                          （不信 agent 自己跑的那次）
      ├─ git checkout -B agent/<slug>-<sha>     （绝不推基线分支）
      ├─ git push -u origin <branch>
      └─ 开 PR（gh 或 REST API），描述里附上验证记录
             └─ --api-base 默认为 https://api.github.com；
                自建实例传 --api-base https://<host>/api/v3
                （也可用环境变量 GITHUB_API_URL）
```

---

## 二、① 独立验证门禁

### 为什么需要两层验证

| 层 | 做什么 | 局限 |
|---|---|---|
| **命令验证** | 跑一条命令，看退出码 | 只证明"测试过了" |
| **独立验证** | 另一个 agent 审这份改动 | 真正在"找茬" |

只有命令验证时，流程是「**实现 agent 自己跑测试 → 自己宣布过了**」——
这正是 `teams/coordinator.py:134` 那条规则要避免的：

> **NEVER let the implementation worker verify its own work.**

因为实现者**锚定在自己的方案上**：只跑能过的那几个测试、看到测试套件通过就放行、
把类型检查报错当"无关"忽略掉。

### 独立性由机械保证（不是靠提示词）

| 维度 | 保证方式 |
|---|---|
| 上下文独立 | 全新 `ConversationManager()`，看不到实现对话 |
| 工具只读 | 注册表里只放 `ReadFile`/`Grep`/`Glob` 等，**`WriteFile`/`Bash` 根本不存在** |
| 权限兜底 | 再加 `PermissionMode.PLAN`，即使注册表被绕过也写不了 |
| 结论必须显式 | 必须输出 `VERDICT: PASS` / `VERDICT: FAIL` |
| **没结论 = 不通过** | 解析不到就按 FAIL（fail-closed） |

### 接入

```python
from mewcode.tools import create_default_registry
from mewcode.tools.create_pr import CreatePRTool
from mewcode.tools.agent_verify import make_subagent_verifier

registry = create_default_registry()
registry.register(CreatePRTool(
    work_dir=os.getcwd(),
    verify_command="python -m pytest tests/ -q",
    artifacts_dir="/artifacts",
    base_ref="main",
    verifier_runner=make_subagent_verifier(agent),   # ★ 独立验证者
    require_independent=True,                        # ★ 没配就拒绝，fail-closed
))
```

### 堵住绕过路径（hook 配置）

模型可能不调 `CreatePR`，而是直接用 `Bash` 跑 `git push`：

```yaml
hooks:
  - id: block-direct-push
    event: pre_tool_use
    if: 'tool == "Bash" && args.command =~ "git\s+push"'
    action:
      type: prompt
      message: 禁止直接 git push，请用 CreatePR 工具。
    reject: true

  - id: block-gh-pr-create
    event: pre_tool_use
    if: 'tool == "Bash" && args.command =~ "(gh|glab)\s+pr\s+create"'
    action:
      type: prompt
      message: 禁止直接开 PR，请用 CreatePR 工具。
    reject: true
```

⚠️ **三个必须知道的坑**（每个都会导致 hook 静默失效）：

| 坑 | 说明 |
|---|---|
| 条件里必须写 `args.command` | 不能写 `command`。`get_field` 只认 `tool` / `event` / `args.*`，写别的**静默不匹配** |
| 正则用 `\s`，**不要写 `[[:space:]]`** | 那是 POSIX 语法，Python 的 `re` 不支持 —— 只报一个 `FutureWarning: Possible nested set`，然后**永远匹配不上**。写这套配置时实测踩过 |
| `reject: true` 时动作必须成功 | `run_pre_tool_hooks` 不检查 `result.success`，任何非 0 退出都会变成拒绝。这正是要的 fail-closed 行为 |

> **这三条配置已经装进本项目了**：`.mewcode/config.local.yaml`。
> 不想要就删掉那个文件（或注释掉对应 hook），不影响其它任何东西。

---

## 三、② 容器化沙箱

### 三条铁律

| # | 铁律 | 不做会怎样 |
|---|---|---|
| **①** | **绝不挂 `docker.sock`** | agent 能 `docker run --privileged -v /:/host` 拿到宿主 root。**这是业界最常见的沙箱穿透** |
| **②** | **绝不挂宿主 `$HOME` / `.ssh` / 云凭证** | 只挂 `/work`（代码）和 `/artifacts`（产物） |
| **③** | **默认禁出网** | 否则 agent 能把代码发出去、也能拉任意代码进来 |

### 「起测试环境」的正确姿势

这是最容易搞错的一点。**环境分三层，只有两层该给 agent**：

| 层 | 例子 | 谁做 | 为什么 |
|---|---|---|---|
| **依赖服务** | PostgreSQL / Redis / mock | **平台预先起好** | 起容器要 docker 权限 → 给了就破沙箱 |
| **项目依赖** | `pip install -r requirements.txt` | ✅ agent | 每个项目不同，且随代码变 |
| **测试数据** | `alembic upgrade head` | ✅ agent | 跟着数据模型走 |

**平台造环境，agent 用环境**：连接串通过环境变量注入，agent 只能"连"不能"起"。

### ⚠️ 这里踩过一个坑：声明了 internal 网络，但没人用它

`compose.test.yml` 原来是这样写的：声明了一个 `test-net: {internal: true}`，
但**三个服务都没有 `networks:` 字段**。compose 于是把它们放到了自动创建的
`mewcode-agent-env_default` 网络上 —— 那个网络 **`internal=false`**。

实测结果：

| 项 | 应有 | 实际 |
|---|---|---|
| 服务所在网络 | `test-net`（internal） | `mewcode-agent-env_default` |
| 网络 internal 标志 | `true` | **`false`** |
| 容器能否连公网 | 不能 | **能连上 `1.1.1.1:443`** |

**铁律③ 完全失效，而当时的 `test_docker_config.py` 照样全绿** ——
因为那条测试只做文本正则：文件里有 `internal: true` 就判定成立。
它断言的是**声明**，不是**效果**。

这和"推送断言空过"是同一类错误。修法：

1. 每个服务显式写 `networks: [test-net]`
2. 加一条**结构化**测试（`test_every_service_joins_the_internal_network`），
   用 YAML 解析后检查每个服务确实加入了那个 internal 网络
3. 加一条 `test_internal_network_name_matches_run_script_default` ——
   因为网络全名对不上时 `run-agent.sh` 会直接 `network not found`
   （这是同一个坑的第二个后果，也实测到了）

负向控制验证过：把 `networks: [test-net]` 从 `mock-http` 上删掉，
新测试立刻失败并指名道姓报出是哪个服务。

修完实测：

```
mewcode-agent-env_test-net  internal=true   容器数=3
从 test-net 里连 1.1.1.1:443  → ✅ 连不上公网（OSError）
db:5432 / redis:6379 / mock-http:8080 → ✅ 都还能连
```

### 沙箱边界自检

```bash
docker run --rm --network mewcode-agent-env_test-net \
  -v repo:/work -v artifacts:/artifacts \
  mewcode-agent sh /app/mewcode/docker/check-sandbox.sh
```

实测输出（14 项全过）：

```
  ✅ /root 不可读（当前非 root）            deny
  ✅ 宿主 ssh 目录不存在                     deny
  ✅ 宿主 ~/.aws 不存在                        deny
  ✅ 宿主 ~/.mewcode 不存在                    deny
  ✅ HOME 下没有 ssh 私钥                      deny
  ✅ 代码目录可写（应该有）              allow
  ✅ 产物出口可写（应该有）              allow
  ✅ 不是 root                                    deny
  ✅ 没有 docker 命令                           deny
  ✅ 没有 docker socket                           deny
  ✅ 没有 kubectl                                 deny
  ✅ 没有 ssh 客户端                           deny
  ✅ 连不上公网（应该连不上）           deny
  ✅ DNS 解析不了外网域名                   deny
  PASS 14 / FAIL 0
  ✅ 沙箱边界成立
```

> 脚本里原来第一条是 `check "宿主 home 不存在" deny test -d /root` ——
> 那是个**永远不可能通过**的断言：`/root` 是基础镜像自带的目录，任何
> Debian/Ubuntu 容器里 `test -d /root` 都为真，所以它只会一直报 FAIL，
> 而 FAIL 的原因跟"宿主 home 有没有被挂进来"毫无关系。
> 一个永远失败的检查会让人开始忽略检查结果，比没有检查更糟。
> 已改成 `test -r /root`（当前非 root 读不读得到）+ 一条 `HOME 下没有 ssh 私钥`。

---

## 四、③ CI 确定性流水线

`ci/apply_and_open_pr.py` —— **这个脚本里没有一行 AI**。

```bash
python ci/apply_and_open_pr.py \
    --repo . --artifacts /artifacts \
    --verify-cmd "python -m pytest tests/ -q" \
    --base main
```

| 模式 | 行为 |
|---|---|
| `--dry-run` | 只 apply + 独立重跑，不推送（CI 里最常用的调试档） |
| `--no-pr` | 推送分支，不开 PR |
| 默认 | 全流程 |

**三个做对的地方**：

1. **独立重跑验证** —— agent 自己跑过，但**不能信**（可能跑错命令、跳测试、改完测试没重跑）
2. **绝不推基线分支** —— 分支名 = `agent/<slug>-<sha>`，且显式检查不等于 base
3. **失败要留痕** —— 写 `FAILURE.md`，并把 agent 自己的验证记录一起附上，
   这样人能看出是「agent 谎报通过」还是「代码真的坏了」

GitHub Actions 工作流在 `ci/agent-pipeline.yml`（含起环境、跑 agent、独立重跑、开 PR、失败贴回 issue、清理）。

---

## 五、跑一遍看效果

```bash
# ★ 端到端：改代码 → 自己的验证门禁 → 产出补丁 → CI 重跑 → push → 开 PR
#   （唯一被桩掉的是"模型说了什么"，其余全是真的）
python docs/devops-agent/demo_full_pipeline.py

# 验证门禁（真 git 仓库 + 真 pytest 退出码，零 API）
python docs/devops-agent/demo_verify_gate.py

# 故障定位链路（MockOpsBackend 的 orders-api 场景，零 API）
python docs/devops-agent/demo_incident_triage.py

# 真实后端：本地真起 HTTP 服务，三个后端对着真 socket 跑完整排查链路
python docs/devops-agent/demo_real_backends.py

# 全量测试
python -m pytest                              # 357 passed, 22 skipped（默认沙箱）
                                              # 378 passed, 1 skipped（允许真实 push 的环境）
python -m pytest tests/test_autonomous_delivery.py  # 42 passed
python -m pytest tests/test_toolset_wiring.py # 58 passed
python -m pytest tests/test_pr_open_http.py   # 16 passed
python -m pytest tests/test_create_pr.py      # 30 passed
python -m pytest tests/test_ci_pipeline.py    # 12 passed
python -m pytest tests/test_docker_config.py  # 24 passed
python -m pytest tests/test_ops_tools.py      # 25 passed
python -m pytest tests/test_ops_backends.py   # 69 passed
docker compose -f docker/compose.test.yml config --quiet   # 语法校验
docker compose -f docker/compose.test.yml up -d --wait     # 起测试环境（实测 Healthy）
```

> ⚠️ 默认沙箱下会有 **22 条 skip**：绝大多数是推送/开 PR 相关的断言被控制组拦住
> （环境建不了具名管道 → `git push` 跑不通 → 如实 skip 而不是假装通过）。
> 想真的跑它们，需要在允许创建具名管道的环境里执行 ——
> 本轮的验证就是在那种环境下跑的（**378 passed**），结果见下。

### 端到端 demo 的输出（节选）

```
③ 装配 toolset —— 这一步以前**不存在**
  create_default_registry() 有 6 个工具
  里面有 CreatePR 吗？没有 ← 这就是缺口
  装配后有 7 个工具，新增：['CreatePR']

④ 先试一次「代码还没改」就提 PR —— 门禁必须拦住
  is_error = True
  ❌ 提 PR 被拒：验证未通过
  产出补丁了吗？没有 ← 正确

⑥ 再提一次 —— 命令验证 + 独立验证都过
  ✅ 命令验证（退出码 0） + 独立验证（VERDICT: PASS）
  独立验证者拿到的工具集：['ReadFile', 'Glob', 'Grep']
  写工具泄漏？没有 ← 只读注册表生效

⑧ 远端此刻的状态（agent 从没推送过）
  commit 数：1（还是 1）

⑨ CI 收尾：真 push + 真开 PR
  ✅ 补丁已应用
  ✅ 独立验证通过（退出码 0）
  ✅ 已提交到 agent/fix-get-tier-e7a1b50
  ✅ 已推送到 origin/agent/fix-get-tier-e7a1b50
  ✅ PR 已创建：https://github.com/acme/orders-api/pull/142

⑩ 验证副作用：远端真的变了，但只多了一个 agent 分支
  commit 数：2
    agent/fix-get-tier-e7a1b50
    * main
  ✅ main 仍指向最初那个 commit —— 基线分支没被碰过

⑪ 验证 PR 请求真的发出去了
  POST /repos/acme/orders-api/pulls
  Authorization: Bearer ghp_demo_token
    title: 'fix: 修 get_tier 判空'
    head: 'agent/fix-get-tier-e7a1b50'
    base: 'main'
  ✅ base 是 main、head 是 agent 分支、认证头正确
```

Demo 输出（节选）：

```
场景 1：把 add 改成减法 → 提 PR
  ❌ 提 PR 被拒：验证未通过
  退出码：1    耗时：3.3s
  >       assert add(1, 2) == 3
  E       assert -1 == 3
  补丁文件是否生成：否 ✅（验证没过就不该产出）

场景 2：改对了 → 提 PR
  ✅ 命令验证（退出码 0）+ 独立验证（VERDICT: PASS）
  补丁已生成：.mewcode/pr/changes.patch

证明：远端一个 commit 都没多
  origin 上的 commit 数：0  （起始为 0，跑完仍是 0）
  origin 上的分支：(空)
```

---

## 六、测试覆盖

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_create_pr.py` | 30 | 验证门禁 / **远端不被改动（带控制组）** / 独立验证 / 边界情况 / 真实验证器 |
| `tests/test_ci_pipeline.py` | 12 | 前置检查 / **重跑失败绝不推送** / **成功时真的推上去了** / 分支名安全 |
| `tests/test_toolset_wiring.py` | 58 | **装配缺口本身** / 配置校验（含 mode）/ 能力清单进 prompt / 权限语义 / 覆盖层合并 |
| `tests/test_autonomous_delivery.py` | 42 | **结构性推不到基线分支** / **验证先于推送** / 三种 mode 语义 / 绝不合并 |
| `tests/test_pr_open_http.py` | 16 | 开 PR 的**真实 HTTP 请求构造** / 认证头 / payload / 错误处理 / gh 优先 |
| `tests/test_ops_tools.py` | 25 | 运维工具 / 权限分类 / 证据链强制 |
| `tests/test_ops_backends.py` | 69 | 日志聚类 / 真实 HTTP 请求构造 / 响应解析 / **「未接入」必须报错** |
| `tests/test_docker_config.py` | 24 | 三条铁律（**没有 docker.sock / 没有宿主凭证 / 默认禁网**）+ 每个服务确实加入了 internal 网络 |
| `tests/test_hardening.py` | — | `mewcode.mcp` 不再遮蔽第三方 `mcp` SDK |

关键几条（都是"证明安全边界成立"的）：

| 测试 | 证明什么 |
|---|---|
| `test_create_pr_is_not_in_the_default_registry` | 装配缺口**真实存在**（内置注册表里确实没有 CreatePR） |
| `test_assembly_actually_registers_create_pr` | 装配之后 CreatePR **真的出现了** |
| `test_branch_without_agent_prefix_rejected` | ★ **结构性护栏**：`guard_branch` 只放行 `agent/` 前缀，所以推不到基线分支 |
| `test_independent_runs_before_anything_is_committed` | ★ **顺序**：验证者被调用时本地还没有任何新提交（验证 → 提交 → 推送） |
| `test_command_verify_failure_pushes_nothing` | 命令验证没过 → 远端一个 commit 都不多 |
| `test_independent_verify_failure_pushes_nothing` | 独立验证 FAIL → 远端零变化 |
| `test_pr_mode_never_merges` | 只开 PR 不合并，`main` 仍指向最初那个 commit |
| `test_pr_mode_without_gh_or_token_is_rejected` | `mode=pr` 开不了 PR → **启动时就报错**，不等到最后一米 |
| `test_verifier_without_verdict_is_fail_closed` | 验证者没给 VERDICT → 按不通过，一个 commit 都不推 |
| `test_readonly_registry_excludes_create_pr` | 独立验证者拿不到 CreatePR —— 否则它会递归提 PR |
| `test_missing_token_env_fails_at_config_load` | 凭据读不到 → **启动时**报错，不是半夜排查时拿 401 |
| `test_validator.py::test_enabled_without_verify_command_is_rejected` | 没配验证命令的 CreatePR 直接拒绝启动 |
| `test_control_real_push_works` | ★ **控制组**：先证明本环境真能 push，负向断言才有判别力 |
| `test_successful_reverify_actually_pushes` | ★ 验证通过时**确实推到远端**了（不是只靠否定式证明） |
| `test_origin_untouched_after_create_pr` | CreatePR 跑完，远端 commit 数**一个都不多** |
| `test_failed_reverify_does_not_push` | CI 重跑失败 → 远端零变化 |
| `test_url_path_is_correct` | PR API 的 URL 拼得对（`{api_base}/repos/{owner}/{name}/pulls`） |
| `test_payload_fields` | PR payload 里 `base` 是基线、`head` 是 agent 分支（**不能自己合自己**） |
| `test_http_error_is_reported_readably` | GitHub 返回 401/422 时原因原样透出来 |
| `test_missing_verdict_is_treated_as_fail` | 验证者忘给结论 → fail-closed |
| `test_command_verify_runs_before_independent` | 命令验证没过就不浪费一次独立验证 |
| `test_missing_capability_raises_not_returns_empty` | 没接部署数据源 → **报错**，不伪装成"没有部署" |
| `test_miswired_provider_fails_at_construction` | 后端参数接错在**启动时**就炸，不是半夜排查时 |
| `test_never_mounts_docker_socket` | 沙箱穿透的第一号入口被封死 |
| `test_every_service_joins_the_internal_network` | ★ **每个服务确实加入了 internal 网络** —— 断言"效果"而不是"声明"（原来的文本断言漏掉了真事故） |
| `test_internal_network_name_matches_run_script_default` | 网络全名和 `run-agent.sh` 默认值一致，否则脚本直接 `network not found` |
| `test_check_script_does_not_assert_on_distro_paths` | 钉住 `test -d /root` 那种**恒真断言**不会写回去 |
| `test_same_error_with_different_ids_collapses` | 1,204 条日志能塌成 1 类（聚类真的有效） |
| `test_values_that_vary_still_collapse` | `3000ms` / `1.5GB` / `v2.14.3` 这些也能归一化 —— 漏一个聚类就爆炸 |
| `test_stale_cluster_id_does_not_silently_resolve_to_another_cluster` | 过期聚类 ID **报错**，不会静默返回另一类错误的日志 |

### ⚠️ 一条自我更正：之前的"已端到端验证"是说过头了

原先我写过「"不推送"这个核心断言已经用真实 bare 仓库端到端验证过了」。**这句话不成立。**

真实情况是：那几条测试只做了 `git remote add origin <本地路径>`，
**从不 clone、也从不 push**。而在这台机器的默认沙箱下，`git push` 根本跑不起来
（Git for Windows 的 `sh.exe` 需要创建具名管道，被沙箱以 `Win32 error 5` 拒绝）：

```
sh.exe: *** fatal error - couldn't create signal pipe, Win32 error 5
```

于是"CreatePR 没有推送"和"CreatePR 尝试推送但失败了"在断言上**完全无法区分** ——
测试会**空过**，而且是绿的。

修法就是加**控制组**：`real_push_supported` 先真的建 bare 远端推一次，推不上去
就让相关断言一起 **skip**（不是通过），并把原因打出来。这和 retention 评测里
"控制组必须 100%"是同一条原则：**没有先证明机制本身可用，负向断言就没有判别力。**

现在这三类断言都在能推送的环境里真实执行过（见下面第七节）。

---

## 七、没做的（诚实说明）

先把**这一轮做完的**列出来，因为之前那张表里有几条已经作废：

| 项 | 状态 |
|---|---|
| 工具装配缺口 | ✅ 已修：`toolset.py` + `config.yaml` 的 `toolset` 段；TUI 和 `-p` 两条路径都接了 |
| **agent 自己推送 + 开 PR** | ✅ 已实现：`mode: patch / push / pr` 三档；`delivery.py` 结构性保证推不到基线分支；验证先于推送有专门测试钉住 |
| 真实 `git push` | ✅ 已实测：升权后真推到 bare 远端，`agent/*` 分支出现、`main` 未被碰 |
| 开 PR 的 HTTP 路径 | ✅ 已实测：`--api-base` 可配置，对着本地假 GitHub API 真发 POST，路径/头/payload 全验证 |
| 端到端整条链路 | ✅ `docs/devops-agent/demo_full_pipeline.py` 一次性跑完并 exit 0 |
| 空过的推送断言 | ✅ 已修：加控制组，环境不支持时如实 skip 而不是假装通过 |
| compose 的 internal 网络 | ✅ 已修：原来声明了却没人用，实测容器能直连公网；现已每个服务显式加入并结构化钉住 |
| 沙箱自检脚本 | ✅ 已修：`test -d /root` 是恒真断言，永远 FAIL；现改为 `test -r /root`，实测 14/14 通过 |
| shell 脚本语法 | ✅ `bash -n` 两个脚本都通过 |
| compose 测试环境 | ✅ `up -d --wait` 实测：db / redis / mock-http 全部 Healthy，内网互通、公网不通 |

仍然没做的：

| 项 | 说明 |
|---|---|
| **模型能不能真的找到并改对那个 bug** | 端到端 demo 里**唯一被桩掉的就是这一步**（用桩 client 返回 `VERDICT`）。要真验需要 LLM，本机 API 余额不足（402）。这是整条链路上最核心的一环，也是唯一没验的一环 |
| **没对着真 GitHub 开过 PR** | 本机没有 `gh`、没有 `GITHUB_TOKEN`、没有 git 凭据。验证的是**请求构造**（对着本地假 API 真发 HTTP），不是 GitHub 服务端行为。补一个 token 就能真验：见下面那句命令 |
| **部署 / 工单后端** | 日志（Loki）、指标（Prometheus）、告警（Alertmanager）已有真实实现；**部署和工单没有通用标准**，需要你写适配器接 ArgoCD / Jenkins / Jira。接口就 8 个方法，见 `docs/devops-agent/mcp-ops.md` |
| **没对着真 Loki/Prometheus 实例跑过** | 单测用 `httpx.MockTransport`（验证请求构造与响应解析），`demo_real_backends.py` 真起 HTTP 服务走真 socket —— 但那个服务是本地的假实现，不是真的 Loki。**真接生产前必须先对着真实例验一次字段名**（各家 `stream` 标签、`severity` 取值可能都不一样） |
| **容器里"agent 改代码"这一段没跑过** | 运行时边界（三条铁律）已用真实标志 + 真实网络实测通过；`docker build` 则卡在**构建容器的因特网出网不可用**上，所以镜像没建出来，容器跑不了 agent。原因与解决办法见下 |

### 补充：构建容器的出网在这台机器上是断的

`docker build` 卡住的真实原因是**构建容器出不了网**（而 Docker 自己的镜像拉取
正常，所以很容易误判成"镜像源挂了"）。实测：

| 目标 | 结果 |
|---|---|
| `http://mirrors.aliyun.com`（直连，已清掉注入的代理） | **502 Bad Gateway** |
| `https://mirrors.tuna.tsinghua.edu.cn` | **SSL UNEXPECTED_EOF_WHILE_READING** |
| `https://deb.debian.org` | **SSL UNEXPECTED_EOF_WHILE_READING** |

两个不同的 HTTPS 主机都报 SSL 被截断、明文 HTTP 报 502，说明中间有东西在拦。
另一条线索是 Docker Desktop 会把宿主的系统代理注入构建容器：

```
connecting to 127.0.0.1:7890: No connection could be made because the
target machine actively refused it
```

容器里的 `127.0.0.1` 是**容器自己**，不是宿主机 —— 那个地址永远连不上，
而这个报错很容易被误读成"网络不通"。所以 Dockerfile 默认把注入的代理清掉
（`APT_HTTP_PROXY` 默认空）；确实需要代理时要用 `host.docker.internal`：

```bash
docker build -f docker/Dockerfile.agent -t mewcode-agent \
    --build-arg DEBIAN_MIRROR=mirrors.aliyun.com \
    --build-arg APT_HTTP_PROXY=http://host.docker.internal:7890 .
```

### 想真验一次"对着真 GitHub 开 PR"，就三条命令

```bash
export GITHUB_TOKEN=ghp_xxx          # 需要 repo 权限
git remote add origin https://github.com/<你>/<仓库>.git
python ci/apply_and_open_pr.py --repo . --artifacts .mewcode/pr \
    --verify-cmd "python -m pytest tests/ -q" --base main
```

---

## 八、文件清单

```
toolset.py                             ★ 装配层：把配置里的能力装进运行中的 agent
delivery.py                            ★ 交付层：提交 / 推 agent/* / 开 PR（agent 与 CI 共用）
config.py 的 ToolsetConfig             toolset 配置模型（ops / create_pr / mode）
validator.py 的 validate_toolset       配置校验（严：错的在启动时就报）
__main__.py / app.py                   两条执行路径都调用 assemble_toolset()

tools/create_pr.py                    CreatePR 工具（验证门禁 + 三种交付模式）
tools/agent_verify.py                 独立验证子 agent（只读注册表 + VERDICT 解析）
tests/test_create_pr.py               30 个测试（含推送控制组）
tests/test_autonomous_delivery.py     42 个测试（★ 结构性护栏 + 验证先于推送）
tests/test_ci_pipeline.py             12 个测试
tests/test_toolset_wiring.py          58 个测试（装配缺口本身）
tests/test_pr_open_http.py            16 个测试（开 PR 的真实 HTTP 请求）
tests/conftest.py 的 real_push_supported  ★ 控制组：证明本环境真能 push
docs/devops-agent/demo_verify_gate.py 验证门禁 demo
docs/devops-agent/demo_full_pipeline.py ★ 端到端 demo（改代码 → 门禁 → CI → push → 开 PR）
docs/devops-agent/README.md           本文档

tools/ops/backend.py                  OpsBackend 协议 + MockOpsBackend（订单故障场景）
tools/ops/tools.py                    9 个运维工具（读=read / 建单=write）
tools/ops/backends/http_backends.py   Loki / Prometheus / Alertmanager 真实实现
tools/ops/backends/composite.py       多后端按方法路由 + 能力自描述
tools/ops/backends/factory.py         ★ 配置 → 后端实例（含 token 从环境变量读）
tests/test_ops_tools.py               25 个测试
tests/test_ops_backends.py            69 个测试
docs/devops-agent/mcp-ops.md          两条接入路径 + 只读凭据分级
docs/devops-agent/demo_real_backends.py 真起 HTTP 服务的后端 demo
docs/devops-agent/skills/…            incident-triage 排查 SOP

docker/Dockerfile.agent               agent 镜像（非 root、无 docker、基础依赖预装）
                                      构建参数：DEBIAN_MIRROR / APT_HTTP_PROXY
docker/compose.test.yml               测试环境（私有网络 + internal: true）
docker/run-agent.sh                   启动脚本（三条铁律都在这）
docker/check-sandbox.sh               沙箱边界自检
tests/test_docker_config.py           21 个测试（三条铁律的回归）

ci/apply_and_open_pr.py               接收补丁 → 独立重跑 → 推送 → 开 PR（无 AI）
                                      --api-base 可指向自建实例（也可用 GITHUB_API_URL）
ci/agent-pipeline.yml                 GitHub Actions 完整工作流
```

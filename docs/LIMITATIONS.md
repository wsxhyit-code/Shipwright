# 已知问题

按"现象 / 影响 / 复现 / 打算怎么修"记录。**未修的都在这里，不在 README 首页** ——
但也不会藏：这一页就是给人查的。

---

## 1. `EditFile` / `WriteFile` 在 Windows 上会改写整个文件的行尾

**现象**：工具用 `Path.write_text(content, encoding="utf-8")` 写回（`tools/edit_file.py`）。
Python 在 Windows 上会把字符串里的 `\n` 写成 `\r\n`，而原来读进来的内容是
`\r\n` 被 universal newlines 归一化成 `\n` 的 —— 于是**整个文件被重写成 CRLF**。

**影响**：

- 改一行代码产生**整文件 diff**（实测：8 行的文件出现 20 行变更）
- `CreatePR` 产出的补丁是纯 CRLF → `git apply` 打不到 LF 仓库上
  （实测报 `patch does not apply`；把补丁归一化成 LF 后立即成功）
- 任何**按字节**校验的东西会坏：哈希清单、`* -text` / `eol=lf` 的仓库、SBOM、签名
- diff 里出现大量模型从未触碰的行，review 时很难看

**复现**：

```bash
git init -b main demo && cd demo
printf 'a\nb\nc\n' > f.txt && git add -A && git commit -m init
# 让 agent 用 EditFile 把 b 改成 B，然后：
git diff --stat        # 3 行文件显示 3 增 3 删
git diff | xxd | head  # 每一行都以 0d 0a 结尾
```

**打算怎么修**：读的时候归一化成 `\n`（保持 `old_string` 匹配行为不变），
写回时按**原文件的行尾**还原；新文件用 LF。`tools/write_file.py` 大概率同病，一起改。

---

## 2. 容器镜像未实际构建

**现象**：`docker build -f docker/Dockerfile.agent` 卡在构建容器的出网不可用上。

**排查结论**（很容易误判成"镜像源挂了"，实测记录）：

| 目标 | 结果 |
|---|---|
| `http://mirrors.aliyun.com`（直连，已清掉注入的代理） | 502 Bad Gateway |
| `https://mirrors.tuna.tsinghua.edu.cn` | SSL UNEXPECTED_EOF_WHILE_READING |
| `https://deb.debian.org` | SSL UNEXPECTED_EOF_WHILE_READING |

两个不同的 HTTPS 主机都被截断、明文 HTTP 报 502，说明中间有东西在拦。
另一条线索是 Docker Desktop 会把宿主系统代理注入构建容器，而容器里的
`127.0.0.1` 是**容器自己**，于是那个代理地址永远连不上 —— 这个报错也容易被误读。

**现状**：镜像没建出来，容器里"agent 改代码"那一段没跑过。
但**运行时边界**（三条铁律）已用真实标志 + 真实网络单独实测通过。

**需要代理时的构建方式**：

```bash
docker build -f docker/Dockerfile.agent -t mewcode-agent \
    --build-arg DEBIAN_MIRROR=mirrors.aliyun.com \
    --build-arg APT_HTTP_PROXY=http://host.docker.internal:7890 .
```

---

## 3. 子 agent 的权限模式取默认值，会静默失败

**现象**：`agents/builtins/` 下 4 个 Agent 定义（Explore / general-purpose / Plan /
Verification）都没有写 `permissionMode`，`agents/parser.py` 的默认值是 `"default"`。
而 `default` 模式下 write / command 都是 `ask`，子 agent 走的又是
**非交互执行路径**（`run_to_completion` → `_execute_tool_noninteractive`），
那里没有"问用户"这条路，遇到 `ask` 只能：

- `dontAsk` → 自动放行
- 其它模式 → 返回 `Permission denied: non-interactive agent cannot prompt user`

**影响**：用 `subagent_type` 派生出来的子 agent，**写文件和跑命令都会被拒**，
而且是**闷声失败** —— 子 agent 的失败不进主对话，用户在界面上什么都看不到。
（只有 `fork` 路径和"不带 `subagent_type` 的 teammate"在代码里硬编码成 `dontAsk`。）

另外 `AskUserQuestion` 在 `ALL_AGENT_DISALLOWED_TOOLS` 里，对所有子 agent 全局禁用 ——
即**子 agent 无法向用户提问**，只能自己判断或失败。

**打算怎么修**：给内置定义补 `permissionMode`，或改 parser 的默认值；
并把"非交互无法询问"这个失败**上升给父 agent**（转成事件或消息），而不是就地拒绝。

---

## 4. 真实 Loki / Prometheus 未对实例验证

单测用 `httpx.MockTransport`（验请求构造与解析），`demo_real_backends.py` 真起 HTTP 服务走真
socket —— 但那个服务是本地假实现。**接生产前必须对真实例验一次字段名**：
各家 `stream` 标签、`severity` 取值可能都不一样。

---

## 5. 部署 / 工单后端没有通用标准

日志（Loki）、指标（Prometheus）、告警（Alertmanager）有真实实现；
**部署和工单**需要自己写适配器接 ArgoCD / Jenkins / Jira。接口就 8 个方法，
见 `docs/devops-agent/mcp-ops.md`。

---

## 6. 工具链坑：`git apply` 打不上补丁时**退出码仍是 0**

不是本项目的代码问题，但会**直接骗过**用它做门禁的脚本：

```powershell
$ git apply -v -p1 changes.patch
Skipped patch 'src/foo.py'.
$ echo $LASTEXITCODE
0
```

文件一个字没改，退出码是 **0** —— 于是"补丁生效了"和"补丁根本没打上"在输出上完全一样。
这和 `docs/devops-agent/README.md` 里反复出现的那类**空过断言**是同一个毛病。

**防御方式**（`eval/shipwright-triage/verify_patch.py` 就是这么做的）：不信退出码，
改成 ① 自己按 unified diff 应用，或 ② 应用前后对文件算 blob 哈希对账，
或至少 ③ 把 `Skipped` 当成错误而不是成功。

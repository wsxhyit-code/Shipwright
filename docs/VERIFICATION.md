# 验证状态与边界

README 只放结论摘要，这里是完整记录。原则是：**说清哪些是实测过的、哪些还只是设计**，
以及**每条是怎么验的** —— 没有"应该没问题"这种表述。

---

## 一、已端到端实测

| 项 | 怎么验的 |
|---|---|
| ★ **独立验证者能抓到埋进去的 bug** | **真实 LLM**（`pytest -m llm`）：植入"缺判空"的 bug → 判 FAIL，且**理由里点到了 `None` / 判空 / `KeyError`** —— 证明它真读懂了代码，不是碰巧 |
| ★ **验证者有区分能力** | 反向用例：实现正确时判 PASS。一个永远 FAIL 的验证者和不存在一样没用，还会堵死流程 |
| 验证者物理上改不了文件 | 只读注册表 + `plan` 模式，真实模型也改不动 |
| 验证门禁拦得住没改好的代码 | `demo_full_pipeline.py`：代码没修 → 被拒 → **不产出补丁** |
| 顺序：验证先于交付 | `tests/test_autonomous_delivery.py` 里有一条专钉这个顺序：验证者被调用时，本地**还没有任何新提交** |
| 绝不推基线分支 | `delivery.guard_branch` 只放行 `agent/` 前缀，对 protected 分支名直接拒绝；有专门测试 |
| 真实 `git push` | 推到真实 bare 远端，`agent/*` 出现、`main` 未被碰（有**控制组**先证明本环境真能 push，否则负向断言会空过） |
| ★ **开 PR 的整条交付链路** | 对着**本地假 GitHub API** 真跑：真 push 到本地 bare 远端 + 真发 `POST {api_base}/repos/{owner}/{name}/pulls`，**路径 / `Authorization` 头 / payload（`base` 是基线、`head` 是 agent 分支）全部核对**。见 `tests/test_pr_open_http.py`（16 条）与 `eval/shipwright-triage/demo_pr.py` |
| 沙箱三条铁律 | 真实运行标志 + 真实网络：容器能连内网服务、连不上公网；`docker/check-sandbox.sh` 自检 14/14 |
| ★ **本地故障演练场：告警 → 定位 → 修复 → 门禁 → 补丁** | `mewcode-incident-lab/` 真实运行：12 次真实请求打出 8 个 500 → 滑窗告警分类为代码故障 → 派给真实 agent → 它自己查到 `app/orders.py` 空列表除零、改对、跑通验收 → `CreatePR` 命令验证 exit 0 + 独立验证 PASS → 产出补丁。见 `INCIDENT-LAB.md` |
| **仓库可安装** | 构建 wheel → 解开 → `import mewcode` + 关键子模块 + `styles.tcss` + 内置 skill 全部正常 |

> **关于"本地假 GitHub"**：`api_base` 可配置是为了**让这条路径可测** ——
> 对着本地服务真发 HTTP，才能验证请求 URL、请求头和 payload 拼得对不对。
> 它**不**证明 GitHub 服务端的行为。要验证后者，需要下面第二节里那些凭据。

---

## 二、未验证 / 有边界

| 项 | 说明 |
|---|---|
| **对着真 GitHub 服务端开 PR** | 需要三样东西同时具备：目标仓库的 `origin`、git 推送凭据（`gh` 登录或 credential helper）、以及开 PR 用的 `GITHUB_TOKEN`。**尚未在真服务端验证过**；本仓库验的是请求构造（对着本地假 API 真发 POST） |
| **容器镜像实际构建** | 构建容器的出网环境受限（实测 `deb.debian.org` / 阿里云镜像都不可达），镜像未建出来。运行时边界（三条铁律）已用真实标志 + 真实网络单独验证 |
| **真实 Loki / Prometheus 实例** | 单测用 `httpx.MockTransport`（验证请求构造与解析），demo 真起 HTTP 服务 —— 但那是本地假实现。**接生产前必须对真实例验一次字段名**（各家 `stream` 标签、`severity` 取值可能不一样） |
| **部署 / 工单后端** | 没有通用标准，需要自己写适配器接 ArgoCD / Jenkins / Jira。接口就 8 个方法 |
| **独立验证者的执行能力** | 它的注册表里只有 `ReadFile` / `Grep` / `Glob`，没有 `Bash`。所以它给的是**静态推理**（"我追了调用链，逻辑成立"），不是"我跑过，绿了"。这是刻意的只读设计，但代价要知道 |
| **子 agent 的权限模式** | 内置的 4 个子 agent 定义没有写 `permissionMode`，于是取默认值 `default`（写/命令 = ask）；而子 agent 走的是非交互执行路径，遇到 `ask` 只会返回 `Permission denied: non-interactive agent cannot prompt user`。另外 `AskUserQuestion` 对所有子 agent 全局禁用 —— 即**子 agent 无法向用户提问**，只能失败或自行判断 |

### 想真验一次"对着真 GitHub 开 PR"

```bash
# 1. 三样前置
git remote add origin https://github.com/<你>/<仓库>.git
export GITHUB_TOKEN=ghp_xxx      # 需要 repo 权限（只用于开 PR 的 REST 调用）
# 推送凭据：gh auth login，或配好 credential helper

# 2. 确定性收尾脚本（它自己 git apply、独立重跑、推分支、开 PR）
python ci/apply_and_open_pr.py --repo . --artifacts .mewcode/pr \
    --verify-cmd "python -m pytest tests/ -q" --base main
```

> ⚠️ `GITHUB_TOKEN` **只用于开 PR 的 REST 请求，不用于 `git push`**。
> 推送走 git 自己的凭据体系，所以只设 token 不够。

---

## 三、关于"如实 skip"

默认沙箱下 `pytest` 会有若干 **skip**：它们是"本环境做不到"的断言
（例如这里 `git push` 建不了具名管道），**如实 skip 而不是假装通过**。

理由和那次自我更正写在 `docs/devops-agent/README.md`：不加控制组的话，
「没有推送」和「尝试推送但失败了」在断言上**无法区分**，测试会**空过**而且是绿的。
所以这些断言先用一个控制组证明"本环境真能 push"，做不到就 skip 并打出原因。

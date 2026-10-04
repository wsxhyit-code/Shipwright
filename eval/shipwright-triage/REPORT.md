# 实验：把一条告警丢给 agent，看它能不能定位到代码那一行并提 PR

**结论：能，一次跑通。** 但它暴露出来的三个问题比"能"更值得看 —— 见第五节。

```
告警（5xx 12.4%，仅 pod-3）
   └─ 部署记录（10:11 v2.14.3 → pod-3）
        └─ 日志聚类（AssertionError，1847 条）
             └─ 代码（policy.py 的缓存 key 漏了 threshold）
                  └─ 改代码 → 跑测试 → CreatePR → 验证门禁 → 补丁
```

---

## 一、这个实验和已有的 demo 有什么不同

仓库里原来已经有一个故障定位 demo（`docs/devops-agent/demo_incident_triage.py`），
但它讲的是 `orders-api` 的**假故事**：脚本临时生成一个 `OrderService.java`，
把 bug 顶到第 142 行，跑完就把目录删掉。

这里换成一个**真实存在、有测试、可复现**的服务：

| | 原 demo | 本次实验 |
|---|---|---|
| 被引用的代码 | 临时生成的玩具文件，跑完即删 | 仓库里真实的 `account-api`，10 个测试 |
| 日志里的堆栈 | 手写字符串 | `traceback.format_exc()` 从真实运行的服务上抓 |
| bug 能不能复现 | 不能（文件都不在了） | 能，`account-api` 里两条命令就能复现 |
| 补丁有没有被独立验证 | 没有 | 有，见第四节 |

"日志里的每一处代码引用都是真的"这件事由 `ops_scenario.py` 保证：
它在构造场景时**真的把故障跑一遍**，把抓到的堆栈塞进日志聚类。
所以"日志说的那行"和"代码里那行"不可能对不上 —— 想让它们对不上，
得先改代码，那就不是同一个场景了。

---

## 二、被修的那个 bug

`account-api` 是一个账户权益接口，v2.14.3 把等级判定从"写死一个阈值"改成
"按调用方给的阈值判定"，因为运营有两套口径：

| 口径 | 门槛 | 用途 |
|---|---|---|
| `STANDARD_THRESHOLD` | 300 分 | 基础权益额度 |
| `VIP_THRESHOLD` | 500 分 | 会员专享资格 |

改动方向是对的，但缓存那一行漏改了：`tier_cache_key(user_id, threshold)`
**只返回 `user_id`**，于是两套口径共用同一个缓存槽。

```python
def tier_cache_key(user_id: str, threshold: int) -> str:
    """
    判定每次都要读一次用户资料，所以按用户分槽，一个用户一条结果。
    """
    return user_id          # ← threshold 进了签名，却没进 key
```

后果只在**积分落在 [300, 500) 的用户**身上发生：

```
account_summary("u-1002")            # u-1002 有 420 分
  ① credit_limit_for(uid, 300)  → 缓存槽 ["u-1002"] = "GOLD"     （420 ≥ 300，对）
  ② is_vip_eligible(uid)        → 命中缓存，拿到 "GOLD"          （按 500 应当是 STANDARD，错）
  ③ forget(uid) 后重判          → "STANDARD"
  ④ assert tier == verified     → AssertionError → 未捕获 → HTTP 500
```

积分 ≥ 500 或 < 300 的用户，两套口径结果一致，缓存命中看不出问题 ——
**这就是"为什么只有部分用户出错"**，也是为什么原来的测试全绿：
测试里每个用户只被问过一次口径。

复现（不需要任何 AI）：

```powershell
cd eval/shipwright-triage/account-api
python -m pytest -q                                   # 10 passed（基线全绿）
python -c "import sys; sys.path.insert(0,'src'); from account_api.service import account_summary; account_summary('u-1002')"
# AssertionError: 等级判定结果和请求口径不一致：缓存命中返回 GOLD，重新判定得到 STANDARD（VIP 口径阈值 500）
```

---

## 三、agent 实际做了什么

一次运行，32 次工具调用，没有人工干预。轨迹在 `run1.log`：

```
[01] Bash          ls -la && cat README.md            ← 先看这是什么项目
[02] ListAlerts    service=account-api                ← SOP 第一步：确认影响面
[03] ListDeploys   service=account-api                ← 谁刚发过
[05] QueryLogs     window=1h level=ERROR              ← 日志聚类
[06] QueryMetrics  metric=http_5xx_rate group_by=pod  ← 关键：只有 pod-3 异常
[07] GetLogSample  cluster_id=C-1 count=5             ← 拉完整堆栈
[09]-[15] ReadFile policy.py / service.py / profiles.py / api.py / 测试   ← 进代码
[17] Bash          python -m pytest -q                ← 先跑一遍基线
[23]-[26] EditFile policy.py                          ← 改缓存 key 与 forget
[27] EditFile      tests/test_account_summary.py      ← 补一条回归测试
[29] Bash          python -m pytest -q                ← 改完再跑
[32] CreatePR      title/description                  ← 交付
```

它给的根因（摘自最终回复）：

> `src/account_api/policy.py:32` — `tier_cache_key` 的 `return user_id` 忽略了
> `threshold`，导致基础口径(300)和 VIP 口径(500)共用一个缓存槽。
>
> **为什么只有部分用户出错**：积分落在 `[300, 500)` 的用户（如 u-1002 的 420 分）
> 基础口径判 GOLD、VIP 口径判 STANDARD……其余用户两个口径结果一致，缓存命中
> 不暴露问题。同时 v2.14.3 只发到 pod-3，所以只有 pod-3 出现 5xx。

它给的修法：缓存 key 改成 `(user_id, threshold)`，`forget()` 相应地清掉该用户
所有口径的槽，并补一条走完整 `account_summary` 路径的回归测试。

**没有改测试来凑绿**，也没碰断言 —— 这一点值得单独确认，因为"把测试改绿"
是最容易的假通过。`git diff` 数得出来：测试文件 **+9 行、−0 行**，
全是新增，没有任何已有断言被删改。（"10 passed" 在修复前后都是 10：
基线的 5 个 `def` 里有一个 parametrize 展开成 5 条，一共 10 条；
新加的这条让 `def` 数变成 6，但用例总数仍是 10 ——
所以**"测试数没变"不能当成"没加测试"的证据**，得看 diff。）

---

## 四、三层验证，逐层都是机械的

| 层 | 谁做的 | 证据 | 结果 |
|---|---|---|---|
| ① 命令验证 | `CreatePR` 自己跑 | `verify.exit_code = 0`，2.59s | 通过 |
| ② 独立验证 | 另一个子 agent（只读工具集 + PLAN 模式） | `verdict = PASS` | 通过 |
| ③ 干净副本重放 | `verify_patch.py` | 见下 | 通过 |

第 ③ 层是这次新加的，因为第 ② 层有个洞：**它自己也承认跑不了命令**。

> 「本环境没有 Bash/执行工具，我无法实际运行 `python -m pytest -q`；
> 以上结论来自静态追踪和测试代码检查。」—— `verification.md`

静态推理不是执行。所以 `verify_patch.py` 做了三件事，**都在干净副本上**：

```
① 补丁应用后，文件内容的 git blob 哈希 == 补丁头 index 行记录的后像哈希
     src/account_api/policy.py        7eaa313 → 30af0f6  ✅
     tests/test_account_summary.py    8af467b → 7a72e93  ✅
② 在副本里重跑验收命令：10 passed，退出码 0                          ✅
③ 负向控制：把新增的那条测试放到**未修复**的代码上跑
     → 退出码 1，AssertionError at policy.py:74                     ✅
   同一份副本上排除新测试后，其余 9 条全绿（说明红的是新测试，不是环境坏了） ✅
```

第 ③ 步是关键。一条"修复前后都通过"的测试是装饰品；
只有当它在旧代码上真的红、在新代码上真的绿，它才钉住了这个 bug。

---

## 五、这次暴露的三个真问题

### 1. Bash 工具的 stdout/stderr 拿不到东西（影响面最大）

在这个沙箱里，`Bash` 对**任何**命令都返回：

```
Error executing command: [WinError 5] 拒绝访问。
```

`echo hello` 也一样。原因是 `tools/bash.py` 用
`asyncio.create_subprocess_shell` + `stdout=PIPE / stderr=PIPE` ——
管道 stdio 就是沙箱的边界（`MCP server 'context7': [WinError 5]` 是同一个原因）。

**后果**：agent 改完代码**无法自己跑测试**。第一次运行时它试了
`python -m pytest -q`、`cmd /c "python -m pytest -q"`、`dir /b`、`pwd && python --version`
五种写法，全部空手而归，最后只能靠读代码推理。

这不只影响 agent 的效率，还**顶到了 `CreatePR` 的要害**：
它的验证门禁和 `git diff` 都是 subprocess + 管道。
一旦这个门禁跑不起来，`CreatePR` 唯一的硬信号就没了 ——
而现在的行为是返回 `退出码 126 / 命令无法执行`，看起来像"验证失败"，
**不像"验证没跑成"**。

建议：给 `Bash` 一个不走管道的降级路径（临时文件重定向 + 读文件回填），
并且在工具描述里把"验证没跑成"和"验证失败"分得更开。

### 2. 独立验证者拿不到执行能力，"独立"变成了"静态"

它只有 `ReadFile / Grep / Glob`，没有 `Bash`。设计上是刻意的（只读注册表 +
PLAN 模式），但结果是它**只能推理，不能验证**：它给出的 PASS 是
"我追了调用链，逻辑上修复成立"，不是"我跑过，绿了"。

这两件事的价值差得很远。独立验证者如果能执行**只读命令**
（`pytest`、`git diff`），它的 PASS 才真的独立于实现者的自述。

### 3. `git apply` 会"跳过"补丁并返回退出码 0

写 `verify_patch.py` 时踩到的，和我正在验证的这个项目是同一类毛病：

```powershell
$ git -C <copy> apply -v -p1 changes.patch
Skipped patch 'src/account_api/policy.py'.
Skipped patch 'tests/test_account_summary.py'.
$ echo $LASTEXITCODE
0
```

文件一个字没改，退出码是 **0**，而 `pytest -q` 照样绿（基线本来就绿）。
于是"补丁生效了"和"补丁根本没打上"在输出上**完全一样** ——
正是 `docs/devops-agent/README.md` 里反复出现的那类空过断言。

所以 `verify_patch.py` 最后**不用 `git apply`**：自己按 unified diff 应用
（纯标准库，20 行），再用补丁头里的 blob 哈希对账。改了就是改了，哈希说了算。

顺带记下同一条路上的另外两个坑（都在脚本注释里）：
`pytest -k <name>` 没匹配到用例时退出码是 **5**，不单独判掉的话
"测试红了"和"测试没跑"看起来一样；`--collect-only -q` 只打印条数不列用例名，
拿两个副本的总数做判据也是错的。

---

## 六、怎么重跑

```powershell
# 前置：依赖在仓库内的 .deps/ 里（全局 site-packages 不可写），见根目录 run.ps1
cd D:\mewcode

# ① 场景自检：日志里的代码引用是不是真的（零 API）
python eval\shipwright-triage\ops_scenario.py

# ② 跑实验（需要 LLM；沙箱下 Bash 不可用，必须在允许管道 stdio 的环境里跑）
#    输出重定向到 eval\shipwright-triage\run1.log
python eval\shipwright-triage\run_triage.py

# ③ 独立验证 agent 交出的补丁（零 API）
#    先建好两个工作目录（沙箱下脚本自己建不了子目录）
New-Item -ItemType Directory -Force -Path eval\shipwright-triage\.verify-work\fixed
New-Item -ItemType Directory -Force -Path eval\shipwright-triage\.verify-work\buggy
python eval\shipwright-triage\verify_patch.py
```

> ②必须在**允许创建具名管道**的环境里跑，否则 Bash 全线返回 WinError 5，
> agent 只能靠读代码推理（结论可能仍然对，但那不是我们要验的东西）。

---

## 七、文件清单

```
eval/shipwright-triage/
├── account-api/                        被修的那个服务（独立 git 仓库，main = 有 bug 的基线）
│   ├── src/account_api/policy.py       ★ bug 所在：tier_cache_key 漏了 threshold
│   ├── src/account_api/service.py      调用顺序（先基础口径，再 VIP 口径）
│   ├── src/account_api/profiles.py     用户积分数据（u-1002 = 420 分，落在故障区间）
│   ├── src/account_api/api.py          HTTP 层（未捕获异常 → 500 + ERROR 日志）
│   └── tests/test_account_summary.py   10 条测试，基线**全绿**（缺口就在这里）
├── ops_scenario.py                     把内置 mock 换成 account-api 真实故障场景
│                                       （日志里的堆栈是真跑出来的，不是手写的）
├── config.yaml                         本次实验的 provider + toolset 配置
├── run_triage.py                       把告警交给 agent（这次实验的自变量）
├── verify_patch.py                     ★ 干净副本重放 + blob 哈希对账 + 负向控制
└── run1.log                            那次运行的完整轨迹（32 次工具调用）
```

生成的交付物（在应用仓库里，已 gitignore）：

```
account-api/.mewcode/pr/changes.patch      补丁（86 行）
account-api/.mewcode/pr/pr.json            PR 元数据 + 两层验证记录
account-api/.mewcode/pr/verification.md    独立验证者的完整分析
```

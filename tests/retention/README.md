# 上下文信息保留评测

测的是**压缩之后，原本在对话里的信息还在不在**。

核心思路一句话：**压缩完，再问一遍，看还答不答得对。**

---

## 快速开始

```bash
# 1) 零成本：验证流程与指标（伪造摘要器，数字不代表真实保留率）
python -m tests.retention.report --strategy naive
python -m tests.retention.report --strategy structured

# 2) 单元测试 + 流程测试（默认全部零成本）
python -m pytest

# 3) 真实 LLM 测量（花钱）
python -m tests.retention.report --real --sizes 12 --probes-per-type 1
python -m pytest -m llm

# 4) 把数据集落盘，人工审阅
python -m tests.retention.report --sizes 8,16,32 --dump .eval-tmp/dataset.json
```

---

## 一、数据集怎么构造

不"找"数据集，而是**自己造对话、自己埋事实、自己知道标准答案**——
因为没有任何公开数据集把「压缩」当成自变量。

生成方式是**半结构化填充**：句子骨架固定，只有事实值由 `values.py` 决定论地填进来。
零 LLM 成本、可无限扩容、位置与类型精确可控。

### 四条构造规则（每条都对应一个会让结果失真的坑）

| 规则 | 做法 | 防的是什么 |
|---|---|---|
| **1. 事实必须伪造且唯一** | 端口用 `8391` 而不是 `8080` | 模型靠先验知识蒙对 → 假阳性 |
| **2. 一个探针一个事实** | 不把"端口+文件"合成一个探针 | 多事实无法归因，不知道丢的是哪个 |
| **3. 位置分区** | `zone=prefix`（会被摘要）/ `zone=keep`（尾部原文） | 分不清"摘要丢了"和"本来就不归摘要管" |
| **4. 长度跨档扫描** | 8k / 16k / 32k tokens | 保留率是长度的函数，单个数字没有意义 |

另外 `generator.self_check` 会硬拦四件事：期望值重复、`expected` 出现在 `question` 里
（判分自我实现）、事实没真正埋进对话、长度不足以触发压缩。

### 7 类信息 + 陷阱

不同信息类型在压缩下的丢失概率差异极大，所以**分类型报告比单一总分可信得多**：

| 类型 | 例 | 为什么容易丢 |
|---|---|---|
| `number` | 端口 `8391`、行号 `457` | 摘要倾向概括成"配置了端口" |
| `negation` | "**不要**动 X" | LLM 摘要系统性省略否定 |
| `identifier` | `_ledger_outbox_814()` | 需字符级精确，摘要会改写 |
| `rationale` | "选了 A 因为 B" | 留得住决策，留不住理由 |
| `todo` | "这个回头再处理" | 极易丢 → 压缩后重复劳动 |
| `tool_args` | `pytest -k xxx` | 摘要只留"跑了测试" |
| `wording` | "我要的是 X **不是** Y" | 模板第 6 条专门为它加的 |
| `trap` | 问一个原文没有的细节 | **反向探针**，用于算幻觉率 |

---

## 二、评测过程

```
阶段 0  注入构造好的对话
阶段 1  强制压缩：auto_compact(..., manual=True)
          ★ manual=True 跳过阈值检查（context/manager.py:749），
            让"何时压缩"完全可控可复现
          ★★ 必须断言压缩真的发生了——否则短对话会让 auto_compact
             静默返回 None，测试以"全部通过"的姿态假成功
阶段 2  导出 4 个变体（唯一区别是"给模型看多少上下文"）
阶段 3  逐个探针提问
          ★★★ tools=[] 禁用工具！
阶段 4  三级判分
```

### 为什么必须禁用工具

`build_compact_messages` 会主动告诉模型：

> 如果你需要压缩前的具体细节，请用 **ReadFile** 读取完整会话记录：`{path}`

（`context/manager.py:444`）

不禁用工具的话，模型会去回读原文，把摘要的真实缺陷**完全掩盖**——
测出来的 100% 是假的。

### 4 个变体 + 排除法

| 变体 | 给模型看什么 | 诊断什么 |
|---|---|---|
| `control` | 原文全给 | **必须 100%**，否则探针本身有问题，整份结果作废 |
| `summary` | 只有摘要文本 | **← 这才是「摘要模板的保留率」** |
| `keep` | 摘要 + keep 尾部原文 | 若这里保住而 `summary` 没保住 → **探针位置标错了** |
| `e2e` | 完整机制（含恢复附件） | 系统整体 |

| control | summary | keep | 结论 |
|---|---|---|---|
| ❌ | — | — | 测试废了，先修探针 |
| ✅ | ❌ | ✅ | 探针埋错区了（应在 `keep`），不该改摘要模板 |
| ✅ | ❌ | ❌ | **摘要模板真的丢了它** ← 要修的就是这个 |
| ✅ | ✅ | ❌ | 正常（keep 只管最近几轮） |

---

## 三、指标

### 主指标

```
IRR = 保留的探针数 / 总探针数        （只统计非陷阱探针）
```

按变体报告（`IRR_summary` 是核心），并按 7 类 + 分区分别报告。

### ⚠️ 联合指标：压缩比（最容易忽略、也最容易被质疑）

**单独报保留率是可以作弊的**：

```
保留率 100% + 压缩比 0.95  →  几乎没压缩，指标毫无意义
保留率 100% + 压缩比 1.01  →  反而是"膨胀"，靠烧更多 token 换来的
保留率 100% + 压缩比 0.20  →  真的牛
```

所以 `Compression Ratio = after_tokens / before_tokens` **必须一起报**，
`format_report` 会在 ≥1.0 时直接打 ❌、>0.9 时打 ⚠️。

### 反向指标：幻觉率

```
Hallucination Rate = 编造出具体值的陷阱探针数 / 陷阱探针总数
```

没有它，一个"什么都肯答"的摘要会显得保留率超高。

### 辅助指标

| 指标 | 防的是什么 |
|---|---|
| `Info Density = 保留事实数 / summary_tokens` | "摘要写超长所以都保住了" |
| `Consistency`（多次采样一致率） | 随机性；不一致说明处于边界 |

---

## 四、已诊断出的缺陷

### 1. `extract_summary` 会让 `<analysis>` 泄漏（`test_extract_summary.py` 的 xfail）

模板声称 `<analysis>` 部分会被丢弃，但函数只在**两个标签都存在**时才剥离：

```python
start = llm_output.find("<summary>")
end = llm_output.find("</summary>")
if start == -1 or end == -1:
    return llm_output        # ← 兜底：整段返回，含 <analysis>
```

模型漏输出 `<summary>` 时（截断 / 格式漂移），本该丢弃的推演会被整段塞进上下文。

### 2. 模板第 6 条只覆盖 user 消息

`SUMMARY_PROMPT` 第 6 条要求"所有**用户**消息原文保留"，
所以埋在 assistant 消息里的 `tool_args` 探针**保不住**——
流水线上表现为 `tool_args` 类型命中率 0%，而其余 6 类 100%。

这不是 bug，是模板覆盖面的真实缺口。

---

## 五、目录结构

```
tests/
  conftest.py                     # fixtures；覆盖 tmp_path 以避开沙箱路径限制
  test_extract_summary.py         # 纯函数测试（含 1 个 xfail 记录已知缺陷）
  test_retention_pipeline.py      # 流程测试：4 变体隔离、排除法、判分器（零成本）
  test_retention_llm.py           # 真实 LLM 测量（pytest -m llm）
  retention/
    schema.py       Probe / ProbeCase
    values.py       伪造值池
    templates.py    7 类半结构化模板
    generator.py    组装对话 + 自检
    summarizers.py  摘要器（伪造 naive/structured + 真实 LLM）
    answerers.py    答题器（字面 Lexical / 真实 LLM）
    graders.py      三级判分（exact / keyword / absent 反向判）
    harness.py      4 变体隔离 + 跑批 + 指标
    report.py       命令行入口
```

---

## 六、特别注意

**`summarizers.py` 里的 `naive_summary` / `structured_summary` 是流程夹具，不是测量工具。**
它们是我写的模板，用它得出的百分比是自证，**没有意义**。
真实保留率只能用 `--real` 或 `pytest -m llm` 测。

它们的价值在于：把整条 pipeline（真实的 `auto_compact`、keep 窗口切分、
`build_compact_messages`、`build_recovery_attachment`）跑通，并验证 4 变体隔离、
判分器、排除法自检、膨胀告警都真的有效。

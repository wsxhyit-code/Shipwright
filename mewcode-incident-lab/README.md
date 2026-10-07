# mewcode 告警驱动修复实验

真实本地 HTTP 请求触发预埋缺陷，运行服务产生真实日志，监控按阈值触发并分类。
代码故障生成任务交给现有 mewcode；修复、独立验证和 PR 使用你的 Agent 实现。
实验服务Python 3.10+且只用标准库；实际运行Shipwright需要Python 3.11+及它原有的依赖。
无需 Docker、云账号或完整 Prometheus 部署。已适配你现有的HTTP运维工具和交付配置。

## 三个场景

| 场景 | 故障注入方式 | 结果 | 处理 |
| --- | --- | --- | --- |
| 代码异常 | 查询没有订单的客户1002 | HTTP 500 + ZeroDivisionError + 源码堆栈 | 代码修复候选；复现后才允许修改 |
| 配置异常 | --database 指向不存在的文件 | HTTP 503 + 路径缺失证据 | 人工处理配置，不修改业务代码 |
| 依赖异常 | 配送依赖9081端口未运行 | HTTP 503 + 连接异常 | 调查依赖地址/网络，不自动改代码 |

数据库1001客户有两笔订单，1003有一笔，1002没有订单。
`/health` 返回200时业务接口仍可能500，用来展示“进程正常不等于业务正确”。
无订单的业务预期在 docs/API_CONTRACT.md 已规定，Agent 无需猜业务规则。

## 首次准备

解压后进入 mewcode-incident-lab，运行：

```bash
python -m lab.init
python -m unittest discover -s tests -v
```

现有3个测试应通过。首次使用可把实验代码提交到自己的新 Git 仓库，
这样服务日志携带真实 commit。若尚未初始化，日志 revision=unversioned，
可以演示诊断，但提交PR前必须建立 Git 基线。

```bash
git init -b main
git add .
git commit -m "Add order service incident lab"
```

若已经是 Git 仓库，跳过 git init；不要把别人的项目目录当作新实验目录。

## 准备实际 Agent 接入

在安装了你现有模型配置的 Python 环境中：

```bash
python -m pip install "git+https://github.com/wsxhyit-code/Shipwright.git"
python -m lab.configure --mode patch
```

保留你原有模型配置，放在用户目录 `~/.mewcode/config.yaml` 或实验目录 `.mewcode/config.yaml`。
生成器只写覆盖层 config.local.yaml 和 permissions.local.yaml，遇到已有文件会拒绝覆盖。
正式PR阶段把config.local.yaml中的mode改成pr，并完成实验业务仓库origin与GitHub认证。
详细说明见docs/INTEGRATION.md。

## 代码故障：持续运行与触发

终端1，启动业务服务：

```bash
python -m app.service
```

终端2，启动持续监控：

```bash
python -m lab.monitor
```

终端3，提供现有运维工具的本地HTTP数据接口：

```bash
python -m lab.evidence
```

它将真实日志/告警转成现有后端可解析的响应，不是完整Loki/Prometheus。

终端4，启动自动任务消费者：

```bash
python -m lab.dispatch --execute
```

只有代码异常候选派发给真实mewcode，分类与调度不使用LLM。

终端5，发送12次真实请求（4次正常，8次空订单）：

```bash
python -m lab.traffic --scenario code
```

终端2应输出 application_candidate、code_fix_candidate=true，
并生成 runtime/alerts/*.json 和 runtime/tasks/*.md。

如果希望先手动检查任务，可以暂不启动dispatch，改用：

```bash
python -m lab.handoff
python -m lab.handoff --execute
```

此终端的 Python 需要能够 import mewcode；可使用 --python 指定 Agent 虚拟环境。
Agent 运行前，配置你自己的模型、权限、工具与交付模式。
任务要求两轮内完成复现、修改、机械验证、独立验证和 CreatePR。
没有远端和鉴权就输出 patch，不能称为已提PR。

## 阈值与状态

每个 service + endpoint + revision 单独统计，排除 /health；每个请求只有一条统计事件。
默认60秒滑动窗口，至少10个请求，至少5个5xx，错误率>=30%，持续2秒触发。
支持 pending→firing 与同一事件去重；活跃事件写入状态文件，重启监控不会重复派发。
只有 application_candidate 生成待交接任务，其他类别输出报告数据而不派修复。
这是一份真实日志规则分类，不是已经完成的根因诊断；资源类需要另接真实系统指标。

```bash
python -m lab.monitor --window 60 --min-requests 10 --min-errors 5 --error-rate 0.3 --hold 2
```

已经触发的告警在窗口内保留旧失败，不会因 Agent 提了PR就消失。
恢复要求旧错误退出窗口且新的成功请求足够，持续5秒；无流量不是恢复。
服务更新后新 revision 的成功流量也可关闭旧告警。
这是本地演示监控，单进程、单实例；内存保留上限10000条，不作为生产监控替代。

## 配置故障

停止先前的服务和监控。使用独立 runtime 避免混合旧样本：

```bash
python -m lab.init --runtime runtime-config
python -m app.service --runtime runtime-config --database runtime-config/not-found.sqlite3
```

另两个终端分别执行：

```bash
python -m lab.monitor --runtime runtime-config
python -m lab.traffic --scenario config
```

应分类 configuration，code_fix_candidate=false；恢复正确 --database 参数即可。

## 依赖故障

停止先前服务和监控，保持9081端口没有依赖服务：

```bash
python -m lab.init --runtime runtime-dependency
python -m app.service --runtime runtime-dependency
```

另两个终端分别执行：

```bash
python -m lab.monitor --runtime runtime-dependency
python -m lab.traffic --scenario dependency
```

应分类 dependency_or_network，code_fix_candidate=false；需进一步确认依赖地址和健康状态。
本实验没有把所有503一律判断为依赖故障，而是使用结构化错误码与证据。

## 修复验证和上线复测

```bash
python -m lab.verify
```

原始版本应返回非0；修复后应返回0。报告在 runtime/reports/verification.md。
此命令会启动临时空闲端口上的测试服务，验证正常和空订单 HTTP 返回。
它只做机械验证，现有独立 Verification agent 和 CreatePR 仍须执行。

人工合并修复分支后，停止旧服务、在合并后的 main 重新启动。
等待默认60秒窗口中的旧错误消退，再发送包含空订单的流量：

```bash
python -m lab.traffic --scenario code
```

12个请求均应200，监控在恢复持续时间后输出 resolved。
没有合并和重新启动时，旧服务仍可能500；提交PR本身不会更新运行进程。

## 自检与集成说明

```bash
python -m unittest discover -s qa -p "test_*.py" -v
python -m qa.selfcheck
```

自检会创建临时服务，触发真实故障，然后仅在临时副本中做参考修复并检查门禁。
它不是 AI Agent 执行，不调用模型、不创建PR，交付包仍保留有bug的初始业务代码。
结果见 docs/SELF_CHECK.json。内部接口适配和PR前提见 docs/INTEGRATION.md。

lab.dispatch支持持续串行派发、告警ID去重、执行超时和结果留档。
服务/监控/evidence/dispatch各启动一个进程；Ctrl+C停止，重新演示请使用新的runtime或干净实验副本。

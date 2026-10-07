# 与现有 mewcode 集成

## 已实现的可验证边界

这个实验包负责真实 HTTP 服务、SQLite 数据、JSONL 日志、滑动窗口告警、分类和任务文件。
`lab.handoff` 只使用你提供的公开 CLI `python -m mewcode -p <任务>`。
已核对 https://github.com/wsxhyit-code/Shipwright 的源码，基线 commit：
`a0edf388b754ba664028668a8bfb090d28cdfcdb`。
配置、HTTP 运维适配器和 CreatePR 已进行真实接口集成检查，见 docs/SHIPWRIGHT_CHECK.json。
没有把机械测试 PASS 当作独立 Verification agent 的 PASS，没有创建或伪造 GitHub PR。

`lab.evidence` 把真实业务日志和真实检测告警转换为现有后端可解析的 HTTP 形状。
不是安装了 Loki/Prometheus/Alertmanager，也不是它们的完整替代；不支持任意 LogQL/PromQL。
原始日志、告警、指标都来自本地实际请求，没有使用 MockOpsBackend 的预写故障数据。

## 正式工具接入

```bash
python -m pip install "git+https://github.com/wsxhyit-code/Shipwright.git"
python -m lab.configure --mode patch
python -m lab.evidence
```

生成 `.mewcode/config.local.yaml` 与 `.mewcode/permissions.local.yaml`。
运行 Agent 的同一 Python 环境需要已有模型配置；可放在 `~/.mewcode/config.yaml`，
也可以把自己已有配置放在实验目录的 `.mewcode/config.yaml`。生成器只写项目覆盖层，
没有 provider 假值、不输出模型密钥、不覆盖已有配置。
模型配置字段沿用 Shipwright 原配置，不需要换模型或另装框架。

默认 patch 便于先验证；GitHub remote、推送和PR认证完成后，将配置中 create_pr.mode 改成 pr。
`CreatePR` 的验证命令固定为 `python -m lab.verify`，独立验证 require_independent=true。
`-p` 默认不能交互确认，演示提供了具体文件/测试命令权限。没有开启 bypassPermissions。
这个本地规则允许 CreatePR 的全部参数，交付范围仍由工具的当前工作目录、基线和 agent/* 规则限定。
用户级规则优先于项目级规则；若你已有全局 deny，生成本地 allow 不会覆盖它。

## 自动从告警派发任务

```bash
python -m lab.dispatch --execute
```

在实验目录运行。默认单任务串行，告警ID写入状态，900秒执行超时。
只有 application_candidate 派发；配置/依赖故障不进入修复。
调度器只把任务交给真实 `python -m mewcode -p`；退出码0仅表示Agent返回，
是否创建PR必须核对 runtime/delivery/pr.json 和Agent输出，不把退出0当作PR成功。
任务输出在 runtime/reports/*-agent.txt。进程崩溃留下running状态，需要人工检查后决定重试。

## 先通过基础工具跑通

在实验仓库根目录，以安装了 mewcode 的 Python 执行：

```bash
python -m lab.handoff
python -m lab.handoff --execute
```

第一条展示任务，第二条实际调用 mewcode。若 mewcode 在另外一个虚拟环境：

```bash
python -m lab.handoff --python /absolute/path/to/agent-env/bin/python --execute
```

Windows 的 --python 可填写该环境 python.exe 的路径。
让 ReadFile/Grep/Bash 先读取任务、日志、API_CONTRACT 与运行对应版本代码。
启动 evidence 并生成配置后，这条CLI已经能够调用现有告警/日志/指标工具。

## 已核对的工具映射

| 现有工具 | 此实验中的数据/动作 |
| --- | --- |
| ListAlerts / GetAlert | evidence /api/v2/alerts；来源 runtime/alerts/*.json |
| QueryLogs / GetLogSample | evidence /loki/api/v1/query_range；来源 runtime/service.jsonl |
| QueryMetrics | evidence /api/v1/query_range；支持4个命名指标，来自实际请求 |
| GetServiceHealth | 未注册健康后端；用HTTP请求访问9080/health，这是liveness |
| ListDeploys / BuildTimeline | 部署能力未接入；版本与时间记录可从原始日志及Git核对 |
| SearchKnowledge / ReadKnowledge | 未接知识库；先用ReadFile读取docs/API_CONTRACT.md与RUNBOOK.md |
| StartTestEnv / StopTestEnv | 当前-p装配未注册这组工具；lab.verify启动临时空闲端口服务 |
| Verification agent | 在独立只读上下文验证补丁、固定验收结果与修复依据 |
| CreatePR | 使用现有 delivery 顺序，agent/* 分支，人工合并 |

## 现有交付关卡怎么接

规范检查 → `python -m lab.verify`（退出码）→ 现有独立验证者 → 提交 → 推送 → PR。

`lab.verify` 自己包含固定验收校验、语法检查、原有测试、固定单元与 HTTP 验收。
它明确输出 independent_agent_verification=NOT_RUN，绝不代替你的独立模型验证。
默认基线代码的验收失败是演示设计，不应跳过失败继续交付。
固定验收与校验清单需要由平台/CI 保持可信并对实现者只读；本地 hash 是演示辅助，
若 Agent 拥有任意 shell 写权限，可以同时改文件与 hash，因此 hash 不能独立充当安全边界。

建议 CI 把 acceptance/ 与 API_CONTRACT 从固定基线取出到实现者不能写的评测目录。
当前演示权限和CreatePR配置已与源码核对；没有给模型Docker权限。

## 真实 PR 的前提

实验代码需要位于你自己的 GitHub 仓库，修复前已提交，origin 已配置，mewcode 的交付凭据有效。
可以先在 GitHub 创建空仓库，再在此目录运行 GitHub 页面给出的 remote/push 指令。
修复目标仓库应是这个实验业务仓库；mewcode 自己的源码仓库不应混入本次业务修复。

验证后由 CreatePR 创建 PR；不要用另一个脚本绕过你的独立验证门禁。
PR 正文应记录告警、根因、复现、修改、验证输出，状态 waiting_review。
人工合并后重新部署服务，再通过新成功请求确认恢复。

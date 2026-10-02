# 运维工具的两条接入路径

「能写代码也能做运维」需要把内部系统接进来。有两条路，**各有各的坑**。

---

## 路径 A：原生工具（推荐，本项目已实现）

`tools/ops/` —— 工具是自己的 Python 类，**category 由你控制**：

```python
from mewcode.tools import create_default_registry
from mewcode.tools.ops import MockOpsBackend, register_ops_tools

registry = create_default_registry()
register_ops_tools(registry, MockOpsBackend())      # 换成你实现的 backend
```

接真实系统只需实现 `OpsBackend` 的 8 个方法：

```python
class OpsBackend(Protocol):
    def list_alerts(self, service="", severity="") -> list[Alert]: ...
    def get_alert(self, alert_id) -> Alert | None: ...
    def query_logs(self, service, window, level="ERROR", pattern="") -> list[LogCluster]: ...
    def get_log_sample(self, cluster_id, count) -> list[str]: ...
    def query_metrics(self, metric, window, group_by) -> list[MetricSeries]: ...
    def list_deploys(self, service="", limit=10) -> list[Deploy]: ...
    def get_service_health(self, service) -> dict: ...
    def create_incident(self, service, title, severity, root_cause, evidence) -> Incident: ...
```

对应关系，以及**哪些已经有现成实现**：

| 方法 | 接哪个系统 | 本项目已实现？ |
|---|---|---|
| `query_logs` / `get_log_sample` | Loki / Elasticsearch / 云日志服务 | ✅ `LokiLogBackend` |
| `query_metrics` | Prometheus / VictoriaMetrics / 云监控 | ✅ `PrometheusMetricBackend` |
| `list_alerts` / `get_alert` | Alertmanager / PagerDuty / 自研告警 | ✅ `AlertmanagerBackend` |
| `list_deploys` | ArgoCD / Jenkins / GitHub Actions / 自研发布平台 | ❌ 无统一标准，需自写适配器 |
| `get_service_health` | K8s / 自研健康检查 | ❌ 同上 |
| `create_incident` | Jira / 自研工单 | ❌ 同上 |

**权限映射是正确的**：查询 → `read`（DEFAULT 下 allow，排查时不弹窗）；
建单 → `write`（DEFAULT 下 ask，需要确认）。

### 已实现的真实后端（`tools/ops/backends/`）

日志、指标、告警这三个都是**标准化 HTTP API**，所以写了通用实现：

```python
from mewcode.tools.ops.backends import (
    AlertmanagerBackend, CompositeOpsBackend, LokiLogBackend, PrometheusMetricBackend,
)

backend = CompositeOpsBackend(
    alerts=AlertmanagerBackend("https://am.internal", token=os.environ["AM_TOKEN"]),
    logs=LokiLogBackend("https://loki.internal", token=os.environ["LOKI_READONLY_TOKEN"]),
    metrics=PrometheusMetricBackend("https://prom.internal"),
    # deploys / health / incidents：没有通用标准，传你自己的适配器
)
register_ops_tools(registry, backend)
```

三个设计点值得单独说：

**① 日志聚类在客户端做。** Loki 只管取回原始行，`log_signature()` 负责把
"每次都不同"的东西（时间戳、UUID、IP、hex、路径、数字）替换成占位符，
再按签名分组。这是「1,204 条日志 → 2 个聚类」能成立的原因 ——
直接返回原始日志就是往上下文里塞 240KB，正好踩上落盘截断那个坑。

聚类结果里 `count` 是全量条数，但 `sample` 只留 3 条 —— 条数用来判断严重性，
样例用来判断是什么错。两者需要的信息量不一样。

**② 没接入的能力必须报错，不能返回空。**

```python
backend = CompositeOpsBackend(logs=LokiLogBackend(...))   # 没接部署
backend.list_deploys("orders-api")
# OpsCapabilityMissing: 部署 能力未接入，`list_deploys` 无法执行。
#   （接 ArgoCD / Jenkins / GitLab CI 的适配器）
#   注意：这不代表「没有部署」，只是没有接入数据源。
```

因为如果返回 `[]`，agent 会读成「这个时间点没有部署」，
然后据此把根因判断到完全错误的方向 —— 而且全程没有任何异常提示。
**故障排查里静默的空结果比报错危险得多。**

`CompositeOpsBackend.describe()` 可以在排查开始前就把能力清单告诉模型，
让它不去依赖一个根本没接的东西：

```
运维后端能力清单：
  ✓ 告警：AlertmanagerBackend
  ✓ 日志：LokiLogBackend
  ✗ 部署：未接入，调用会报错（≠ 没有部署）
```

**③ 参数写错在启动时就炸。** 构造时会检查每个后端的签名 ——
把 Loki 传给 `metrics=` 不会等到半夜排查故障时才发现：

```python
CompositeOpsBackend(metrics=LokiLogBackend(...))
# OpsError: metrics 后端 LokiLogBackend 缺少 指标 能力所需的 `query_metrics` 方法
```

测试在 `tests/test_ops_backends.py`（69 个），用 `httpx.MockTransport`
拦截 HTTP —— 不用起 Loki/Prometheus/Alertmanager，但走的是**真实的请求构造
和响应解析**。另外 `docs/devops-agent/demo_real_backends.py` 会在本地真起一个
HTTP 服务，把三个后端对着真 socket 跑一遍完整排查链路。

### 写这三个后端时踩到的两个真坑

这两个都不是"写法偏好"，是**不修就一定出错**。

**坑 A：数字后面跟单位时归一化会漏掉。**

日志里最常见的形态是 `charge timeout after 3000ms`。用 `\b\d+\b` 去匹配数字
**匹配不上** —— `3000` 的 `0` 和 `ms` 的 `m` 之间没有词边界（都是 `\w`）。
结果就是每条超时日志都保留了自己的毫秒数，37 条超时裂成了 **37 个聚类**，
聚类等于完全没做。`1.5GB` / `15s` / `1204req` 全是同一类问题。

正确写法是只要求「前后不是数字」，并且**保留单位**
（否则 `3000ms` 和 `3000MB` 会混成一类）：

```python
_NUM_RE = re.compile(r"(?<![0-9.])\d+(?:\.\d+)?(?![0-9])")   # 3000ms → <N>ms
```

版本号要单独一条规则放在前面，否则 `v2.14.3` 只会被换掉第一段：

```python
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){2,}\b")          # v2.14.3 → <VER>
```

回归测试是 `test_values_that_vary_still_collapse`（8 组参数化）。

**坑 B：httpx 默认会走系统代理，而且它读的是注册表。**

httpx 默认 `trust_env=True`。在 Windows 上它会调 `urllib.request.getproxies()`，
**这个函数读注册表**（`HKCU\...\Internet Settings`），不是读环境变量。

所以本机装了 Clash / v2ray（监听 7890 那类）时，对 `127.0.0.1` 的请求也会被
发到代理，代理回一个 **502 空响应** —— 报错信息里没有任何线索。
实测的现象是：裸 socket 拿 200，同一个进程里 httpx 拿 502，
两个请求唯一的差别是 `trust_env`。诊断时 `os.environ` 里**一个代理变量都没有**，
很容易误判成服务端问题。

对内部运维系统来说走代理本来就是错的：

1. **会坏** —— `loki.internal` 这类内网地址代理根本解析不了
2. **会漏** —— 查询语句和 Bearer token 都会经过第三方

所以这三个后端默认 `trust_env=False`。确实需要走代理访问公网托管服务
（Grafana Cloud 之类）时显式传 `trust_env=True`。

**坑 C：聚类 ID 不能用位置序号。**

`GetLogSample` 是按 ID 查缓存的，而缓存只保留**最近一次** `QueryLogs` 的结果。
查完 ERROR 再查 WARN，如果 ID 是位置序号，ERROR 的 `C-1` 会**静默**变成
WARN 的 `C-1` —— agent 拿到的是另一类错误的原始行，而且完全不会察觉。

改成由签名派生（`C-` + sha1(签名) 前 6 位）之后：

- 同一个错误在不同窗口 / 不同查询里 ID **相同**，可以直接跨查询对照
- 过期的 ID 会**查不到**（报错），而不是错配到别的聚类

回归测试是 `test_stale_cluster_id_does_not_silently_resolve_to_another_cluster`。

> 坑 B 和坑 C 是同一个模式的两次出现：**错误的东西悄悄通过，正确的东西才报错**。
> 这和「未接入的能力必须抛错」是同一个原则 —— 在故障排查里，
> 一个看起来正常的错误答案比一个明确的错误信息危险得多。


---

## 路径 B：MCP

```yaml
mcp_servers:
  - name: loki
    url: https://loki.internal/mcp
    headers:
      Authorization: "Bearer ${LOKI_READONLY_TOKEN}"
  - name: prometheus
    url: https://prom.internal/mcp
    headers:
      Authorization: "Bearer ${PROM_READONLY_TOKEN}"
  - name: ci
    command: npx
    args: ["-y", "@your/ci-mcp"]
```

### ✅ 坑 1（已修复）：所有 MCP 工具都被标成 `command`

原来的 `mcpclient/tool_wrapper.py` 无条件写死：

```python
self.category = "command"        # ← 无论这个工具是读还是写
```

`command` 类意味着：

| 后果 | 说明 |
|---|---|
| 不走 `PathSandbox` | 作用域检查对运维工具本来也没意义 |
| 只受那 8 条 **shell 危险正则**约束 | 而那些正则是给 shell 写的，**对运维 API 完全无效** |
| DEFAULT 模式下 `ask` | 每次查询都弹窗 —— 排查要连查十几次，体验很差 |

**两个后果叠加**：既没有真正的护栏，又把排查流程搞得很难用。

**现已修复**：`MCPToolWrapper` 接受 `category` 参数，由 server 配置决定，
语义和原生工具完全一致：

```yaml
mcp_servers:
  - name: loki
    url: https://loki.internal/mcp
    category: read          # ← 读类：DEFAULT 下 allow，排查不弹窗
    headers:
      Authorization: "Bearer ${LOKI_READONLY_TOKEN}"
  - name: ci
    url: https://ci.internal/mcp
    category: write         # ← 写类：DEFAULT 下 ask
```

取值只允许 `read` / `write` / `command`，非法值**回退到 `command`**
（最保守的一档，而不是最宽松的）。`category: read` 同时让
`is_concurrency_safe = True`，只读工具可以并发发起。

改动面：`mcpclient/tool_wrapper.py` + `mcpclient/manager.py` +
`config.py`（`MCPServerConfig.category`）+ `validator.py`（校验）。

> 顺便说明：这个包原来叫 `mewcode/mcp/`，会**遮蔽第三方 `mcp` SDK**
> （`from mcp import ClientSession` 导到的是自己），已重命名为
> `mewcode/mcpclient/`。回归测试在 `tests/test_hardening.py`。


### ⚠️ 坑 2：MCP 工具没有"证据链强制"这类业务校验

原生工具里 `CreateIncident` 会拒绝没有证据的工单：

```python
if not evidence.strip():
    raise OpsError("必须提供证据链（日志/指标/部署的具体线索），不接受无依据的根因")
```

**MCP 工具做不到这一点** —— 它只是把参数转发给远端，校验在远端。
如果你的工单系统不校验，agent 就会建出"根因：服务异常"这种没信息量的单子。

---

## 只读凭据分级（最关键的实践）

**运维工具必须从只读凭据起步。** 这不是保守，是因为：

- 运维动作的**裁判延迟很长** —— 改完配置要等生效才知道对不对
- 而回滚、重启这类动作**不可逆**，错了就是二次故障

分三级接入：

| 阶段 | 凭据 | 能做什么 | 风险 |
|---|---|---|---|
| **第 1 级**（起步，用这个） | **只读** token（只给 `query`/`list` 权限） | 查日志、查指标、看部署状态、看健康 | 零 |
| **第 2 级** | 只读 + 配置 diff（能读部署配置，**不能 apply**） | 对比期望配置和实际配置的差异 | 低 |
| **第 3 级** | 写权限（重启、改配置、扩缩容） | —— | **高，必须人工确认** |
| ~~第 4 级~~ | ~~回滚、删除~~ | **不要给 agent** | 这类动作走 CI 流水线的确定性流程 |

**为什么第 3 级也要谨慎**：`docker/run-agent.sh` 里已经把容器关进了容器，
但应用层的写操作仍然要靠"人工确认"（`category="write"` → `ask`）兜底。
两层都在，才敢放开第 3 级。

---

## 对照表

| | 路径 A（原生工具） | 路径 B（MCP） |
|---|---|---|
| 改动量 | **已有现成实现**（Loki/Prom/AM），部署与工单写适配器 | 改配置 |
| 权限 category | ✅ 可控（读=allow / 写=ask） | ✅ 可控（`category` 配置项，已修复） |
| 业务校验 | ✅ 可以写（如强制证据链） | ⚠️ 依赖远端 |
| 只读凭据 | 由你的实现决定 | ✅ 配置里指定 |
| 日志聚类 | ✅ 客户端做（`log_signature`），上下文可控 | ❌ 远端返回什么就是什么 |
| 「未接入」语义 | ✅ 抛错，不会伪装成"没有异常" | ❌ 工具不存在，模型可能自行脑补 |
| 适用 | **生产环境** | 原型验证 / 已有成熟 MCP server |

**建议**：原型阶段用 MCP 快速验证场景；上生产走路径 A ——
不只是因为权限语义，更因为**日志聚类和「未接入≠没有」这两件事只有
路径 A 能做**，而它们直接决定 agent 的结论对不对。


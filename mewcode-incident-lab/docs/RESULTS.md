# 本次验证范围

Shipwright 基线：a0edf388b754ba664028668a8bfb090d28cdfcdb。

已验证：

- 真实 HTTP 请求触发空订单除零故障；/health=200，而业务接口=500。
- 错误数据库路径返回503，分类configuration，不派发代码修复。
- 配送依赖连接失败返回503，分类dependency_or_network，不派发代码修复。
- 阈值最小样本、错误率、持续时间、告警去重、无流量不恢复、新版本恢复。
- 原有业务测试通过，但固定空订单验收失败；临时副本参考修复后验收通过。
- 原版 Shipwright ListAlerts、QueryLogs、GetLogSample、QueryMetrics 调用实际HTTP接口读取真实证据。
- toolset配置与源代码schema一致；具体编辑/测试/CreatePR权限规则检查通过。
- 原版CreatePR在修复前拒绝；修复后缺少独立验证者仍拒绝，没有绕过独立门禁。
- 原仓库的 test_ops_backends、test_ops_tools、test_toolset_wiring、test_create_pr、test_autonomous_delivery 测试通过。

未执行：

- 没有模型provider配置，因此没有运行真实LLM修复或独立LLM验证。
- 没有实验业务仓库remote/推送/PR凭据，因此没有真实GitHub PR。
- 未部署生产服务；本地缺陷是为演示预埋，异常、HTTP响应和日志是实际执行产生的。

SELF_CHECK.json与SHIPWRIGHT_CHECK.json记录实际检查输出。
参考修复仅用于QA，不代表Agent自动修复；交付包中的app/orders.py仍保留初始缺陷。

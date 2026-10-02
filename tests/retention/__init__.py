"""上下文信息保留评测套件（context information retention evaluation）。

模块分工：
  schema.py       —— Probe / ProbeCase 数据结构
  values.py       —— 伪造值池（防"模型靠先验知识蒙对"）
  templates.py    —— 7 类探针的半结构化填充模板
  generator.py    —— 组装对话、撑到目标长度、自检
  summarizers.py  —— 摘要器（伪造 / 真实 LLM）
  answerers.py    —— 答题器（字面 / 真实 LLM）
  graders.py      —— 三级判分
  harness.py      —— 4 变体隔离 + 跑批 + 指标汇总
"""

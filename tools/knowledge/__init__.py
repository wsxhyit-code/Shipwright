"""企业知识库接入：让 agent 能查内部规范 / SOP / 架构说明。

方向二要的是「接入企业内部的知识库、代码规范、私有工具链，让 agent
写出来的代码天然符合内部标准」。此前项目里**没有任何检索能力**
（0 embedding、0 向量库、0 RAG），最接近的只有 `MEWCODE.md` 静态注入
和 `memory/` 会话记忆。

这个包补上那一块：

    SearchKnowledge     → 按问题检索，只回命中的片段
    ReadKnowledge       → 针对某一条取回完整内容（有上限）
    ListKnowledgeDocs   → 列出知识库里有哪些文档（探路用）

用法（配置驱动，不用写代码）：

    toolset:
      knowledge:
        kind: local                 # 开箱可用：扫本地 markdown
        extra_dirs: ["docs/standards"]
      # 或接内部检索 API：
      # knowledge:
      #   kind: http
      #   base_url: "https://kb.internal/api"

## 两条必须记住的设计

**① 中文按字符二元组切词，不按空格。**
按空格切的话一整段中文会变成一个 token，永远匹配不上 —— 见 `tokenize()`。

**② 「没接入」和「没查到」严格区分。**
没接后端抛 `KnowledgeUnavailable`；接了但确实没有才返回空。
混淆的后果是 agent 把「没接入」读成「内部没有这条规定」，
然后凭自己的习惯写代码，还以为符合内部标准。
"""
from mewcode.tools.knowledge.backend import (
    KnowledgeBackend,
    KnowledgeDoc,
    KnowledgeError,
    KnowledgeHit,
    KnowledgeUnavailable,
    MockKnowledgeBackend,
    tokenize,
)
from mewcode.tools.knowledge.tools import (
    KNOWLEDGE_TOOLS,
    ListKnowledgeDocsTool,
    ReadKnowledgeTool,
    SearchKnowledgeTool,
    register_knowledge_tools,
)

__all__ = [
    "KNOWLEDGE_TOOLS",
    "KnowledgeBackend",
    "KnowledgeDoc",
    "KnowledgeError",
    "KnowledgeHit",
    "KnowledgeUnavailable",
    "ListKnowledgeDocsTool",
    "MockKnowledgeBackend",
    "ReadKnowledgeTool",
    "SearchKnowledgeTool",
    "register_knowledge_tools",
    "tokenize",
]

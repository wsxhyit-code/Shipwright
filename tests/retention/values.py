"""伪造值池。

**这是整套评测最重要的一条防作弊规则**：探针里的事实必须是伪造且唯一的。

  端口用 8080  →  模型靠先验知识就能猜对  →  假阳性，测的其实是模型的常识
  端口用 8391  →  答对 == 真的从上下文里读到了  →  可信

所以这里的取值全部由 (case_id, probe_index) 决定论地派生，跨调用稳定、跨用例唯一。
"""
from __future__ import annotations

import hashlib
import random

_ADJ = (
    "relay", "buffer", "ledger", "cursor", "beacon", "quorum",
    "lattice", "shard", "plume", "tundra", "harbor", "vertex",
)
_NOUN = (
    "sync", "index", "probe", "guard", "digest", "anchor",
    "stream", "catalog", "vault", "prism", "outbox", "fanout",
)

# 这些端口太常见，模型可能靠常识蒙对，必须排除。
_COMMON_PORTS = frozenset({3000, 5000, 5432, 6379, 8000, 8080, 8443, 8888, 9000, 9090})


def _rng(*parts: str) -> random.Random:
    seed = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return random.Random(int(seed[:16], 16))


def fake_port(*parts: str) -> str:
    r = _rng("port", *parts)
    while True:
        port = r.randrange(7300, 9900)
        if port not in _COMMON_PORTS:
            return str(port)


def fake_ident(*parts: str) -> str:
    """伪造函数/符号名，例如 _quorum_guard_412。"""
    r = _rng("ident", *parts)
    return f"_{r.choice(_ADJ)}_{r.choice(_NOUN)}_{r.randrange(100, 999)}"


def fake_file(*parts: str) -> str:
    """伪造文件名，例如 lattice_outbox.py。"""
    r = _rng("file", *parts)
    return f"{r.choice(_ADJ)}_{r.choice(_NOUN)}.py"


def fake_service(*parts: str) -> str:
    r = _rng("service", *parts)
    return f"{r.choice(_ADJ)}-{r.choice(_NOUN)}"


def fake_line(*parts: str) -> str:
    return str(_rng("line", *parts).randrange(40, 900))


def fake_count(*parts: str) -> str:
    return str(_rng("count", *parts).randrange(3, 97))


def fake_qps(*parts: str) -> str:
    return str(_rng("qps", *parts).randrange(11, 89))


def fake_timeout(*parts: str) -> str:
    # 刻意避开 30/60/120 这类默认值
    return str(_rng("timeout", *parts).randrange(37, 199))


def fake_commit(*parts: str) -> str:
    r = _rng("commit", *parts)
    return "".join(r.choice("0123456789abcdef") for _ in range(7))


def fake_reason(*parts: str) -> str:
    """伪造一个唯一的技术理由短语，例如 quorum/tundra 写放大。"""
    r = _rng("reason", *parts)
    return f"{r.choice(_ADJ)}/{r.choice(_NOUN)} 写放大"


def fake_option(*parts: str) -> str:
    r = _rng("option", *parts)
    return f"{r.choice(_ADJ)}-{r.choice(_NOUN)}-v{r.randrange(2, 9)}"


def fake_module(*parts: str) -> str:
    r = _rng("module", *parts)
    return f"{r.choice(_ADJ)}_{r.choice(_NOUN)}"

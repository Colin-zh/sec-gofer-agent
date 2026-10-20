"""向量后端的方言判断与 embedding 序列化辅助。

SQLite 后端不生成向量（仅 FTS5）；opengauss / postgresql 后端额外维护原生
vector 列与 HNSW 索引做 ANN。原生向量列与索引的 DDL 在 opengauss 环境单独
验证（见 schema 模块 docstring）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

VECTOR_DIALECTS = frozenset({"opengauss", "postgresql"})


def is_vector_dialect(dialect_name: str) -> bool:
    """返回该 SQLAlchemy 方言是否启用原生向量检索。"""
    return dialect_name in VECTOR_DIALECTS


def encode_embedding(embedding: Sequence[float]) -> str:
    """把浮点向量编码为 `[x,y,...]` 文本，供 JSON 列与 vector 字面量复用。"""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def decode_embedding(raw: str | None) -> tuple[float, ...] | None:
    """把 JSON 编码的向量解码回元组；空值返回 None。"""
    if raw is None:
        return None
    data = json.loads(raw)
    return tuple(float(x) for x in data)

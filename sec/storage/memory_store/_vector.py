"""向量后端的方言判断与 embedding 序列化辅助。

SQLite 后端不生成向量（仅 FTS5）；opengauss / postgresql 后端额外维护原生
vector 列与 HNSW 索引做 ANN。原生向量列与索引的 DDL 在 opengauss 环境单独
验证（见 schema 模块 docstring）。
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Sequence

from sqlalchemy import inspect
from sqlalchemy.exc import SAWarning

VECTOR_DIALECTS = frozenset({"opengauss", "postgresql"})


def is_vector_dialect(dialect_name: str) -> bool:
    """返回该 SQLAlchemy 方言是否启用原生向量检索。"""
    return dialect_name in VECTOR_DIALECTS


def reflect_column_names(connection, table_name: str) -> frozenset[str]:
    """返回表列名集合；反射时抑制未识别原生 ``vector`` 类型的告警。

    opengauss / postgresql 的 ``embedding_vec vector(...)`` 列不被 SQLAlchemy
    方言识别，``get_columns`` 会发出 ``SAWarning`` 并退回 ``NullType``；此处
    只需列名，因此定向抑制该告警，避免启动日志里的无害噪声。
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Did not recognize type 'vector'",
            category=SAWarning,
        )
        return frozenset(
            column["name"] for column in inspect(connection).get_columns(table_name)
        )


def encode_embedding(embedding: Sequence[float]) -> str:
    """把浮点向量编码为 `[x,y,...]` 文本，作为原生 vector 列的字面量。"""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def decode_embedding(value: str | Sequence[float] | None) -> tuple[float, ...] | None:
    """把向量列返回的文本或数值序列解码为不可变浮点向量。"""
    if value is None:
        return None
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Sequence) or isinstance(decoded, (str, bytes)):
        raise ValueError("embedding must be a JSON array or numeric sequence")
    embedding: list[float] = []
    for item in decoded:
        if isinstance(item, bool):
            raise ValueError("embedding values must be finite numbers")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding values must be finite numbers") from exc
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite numbers")
        embedding.append(number)
    return tuple(embedding)

"""声明数据库 SemanticStore adapter 的领域表结构。

sec_memories 保存抽取出的持久事实；SQLite 后端额外维护 FTS5 虚拟表做关键词
检索，opengauss / postgresql 后端额外维护原生 vector 列与 HNSW 索引做 ANN。
vector 列与 HNSW 索引的 DDL 沿用仓库「可选依赖 + 生产环境单独验证」的立场。
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Connection

from sec.storage.memory_store._vector import is_vector_dialect, reflect_column_names

metadata = MetaData()

memories = Table(
    "sec_memories",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("scope", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("subject", Text, nullable=True),
    Column("content", Text, nullable=False),
    Column("dedup_key", String(64), nullable=False),
    Column("origin", String(16), nullable=False),
    Column("source_session_id", String(255), nullable=True),
    Column("source_entry_id", String(255), nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("scope", "kind", "dedup_key", name="uq_sec_memories_dedup"),
)

FTS_TABLE = "sec_memories_fts"
VECTOR_COLUMN = "embedding_vec"


def create_or_upgrade_schema(
    connection: Connection,
    *,
    vector_dim: int = 1536,
) -> str:
    """创建 SemanticStore 表，并按方言补齐 FTS5 / 原生向量索引；返回方言名。"""
    metadata.create_all(connection)
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
            "memory_id UNINDEXED, searchable)"
        )
    elif is_vector_dialect(dialect):
        _ensure_vector_column(connection, memories.name, vector_dim)
    return dialect


def _ensure_vector_column(
    connection: Connection,
    table_name: str,
    vector_dim: int,
) -> None:
    columns = reflect_column_names(connection, table_name)
    if VECTOR_COLUMN in columns:
        return
    connection.exec_driver_sql(
        f"ALTER TABLE {table_name} ADD COLUMN {VECTOR_COLUMN} vector({vector_dim})"
    )
    connection.exec_driver_sql(
        f"CREATE INDEX IF NOT EXISTS idx_{table_name}_embedding ON {table_name} "
        f"USING hnsw ({VECTOR_COLUMN} vector_cosine_ops)"
    )

"""声明数据库 EpisodicStore adapter 的领域表结构。

sec_episodic 保存进入情景检索索引的原始对话消息；SQLite 后端额外维护 FTS5
虚拟表，opengauss / postgresql 后端额外维护原生 vector 列与 HNSW 索引。
"""

from __future__ import annotations

from sqlalchemy import Column, Float, Index, MetaData, String, Table, Text
from sqlalchemy.engine import Connection

from sec.storage.memory_store._vector import is_vector_dialect, reflect_column_names

metadata = MetaData()

episodes = Table(
    "sec_episodic",
    metadata,
    Column("id", String(255), primary_key=True),
    Column("session_id", String(255), nullable=False),
    Column("entry_id", String(255), nullable=False),
    Column("text", Text, nullable=False),
    Column("timestamp", Float, nullable=False),
    Column("kind", String(32), nullable=False, server_default="message"),
    Index("ix_sec_episodic_session", "session_id"),
)

FTS_TABLE = "sec_episodic_fts"
VECTOR_COLUMN = "embedding_vec"


def create_or_upgrade_schema(
    connection: Connection,
    *,
    vector_dim: int = 1536,
) -> str:
    """创建 EpisodicStore 表，并按方言补齐 FTS5 / 原生向量索引；返回方言名。"""
    metadata.create_all(connection)
    _ensure_kind_column(connection, episodes.name)
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
            "episode_id UNINDEXED, searchable)"
        )
    elif is_vector_dialect(dialect):
        _ensure_vector_column(connection, episodes.name, vector_dim)
    return dialect


def _ensure_kind_column(connection: Connection, table_name: str) -> None:
    """为既有表补齐 kind 列（新建表已由 create_all 包含该列）。"""
    columns = reflect_column_names(connection, table_name)
    if "kind" in columns:
        return
    connection.exec_driver_sql(
        f"ALTER TABLE {table_name} ADD COLUMN kind VARCHAR(32) NOT NULL DEFAULT 'message'"
    )


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

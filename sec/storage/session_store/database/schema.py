"""声明数据库 SessionStore adapter 的领域表结构。

表只保存版本化 Session 历史；底层 Engine、连接池和事务由共享 Database
统一管理，外部工具副作用不属于数据库事务。
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    inspect,
    MetaData,
    select,
    String,
    Table,
    Text,
    update,
)
from sqlalchemy.engine import Connection

metadata = MetaData()

sessions = Table(
    "sec_sessions",
    metadata,
    Column("session_id", String(255), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("active_leaf_id", String(255), nullable=True),
    CheckConstraint("version >= 0", name="ck_sec_sessions_version_nonnegative"),
)

entries = Table(
    "sec_session_entries",
    metadata,
    Column(
        "session_id",
        String(255),
        ForeignKey("sec_sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("sequence", Integer, nullable=False),
    Column("entry_id", String(255), primary_key=True),
    Column("parent_id", String(255), nullable=True),
    Column("payload", Text, nullable=False),
    ForeignKeyConstraint(
        ["session_id", "parent_id"],
        ["sec_session_entries.session_id", "sec_session_entries.entry_id"],
    ),
    CheckConstraint("sequence >= 0", name="ck_sec_session_entries_sequence_nonnegative"),
)


def create_or_upgrade_schema(connection: Connection) -> None:
    """创建 SessionStore 表，并将旧 sessions 表升级到当前结构。

    旧 schema 未保存活动叶节点；升级时以最后写入的条目作为恢复分支。
    """
    metadata.create_all(connection)
    session_columns = {
        column["name"] for column in inspect(connection).get_columns(sessions.name)
    }
    if "active_leaf_id" in session_columns:
        return

    connection.exec_driver_sql(
        "ALTER TABLE sec_sessions ADD COLUMN active_leaf_id VARCHAR(255)"
    )
    latest_entry_id = (
        select(entries.c.entry_id)
        .where(entries.c.session_id == sessions.c.session_id)
        .order_by(entries.c.sequence.desc())
        .limit(1)
        .scalar_subquery()
    )
    connection.execute(update(sessions).values(active_leaf_id=latest_entry_id))

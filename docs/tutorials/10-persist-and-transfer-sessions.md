# 持久化和迁移 Sessions

Client 默认可使用进程内存储；它适合临时对话，但进程退出后无法恢复。跨进程恢复请使用数据库 SessionStore。

## 配置存储后端

`.env.example` 展示了三种配置，实际使用时只启用其中一种。

### 仅当前进程

```dotenv
SEC_SESSION_STORE_BACKEND=in_memory
SEC_DATABASE_URL=
```

进程退出后，Session 历史不会保留。

### 本地 SQLite

项目的 `.env.example` 包含 SQLite 配置：

```dotenv
SEC_SESSION_STORE_BACKEND=database
SEC_DATABASE_URL=sqlite+aiosqlite:///sec.db
```

这是项目默认配置模板中的示例。

### openGauss

```dotenv
SEC_SESSION_STORE_BACKEND=database
SEC_DATABASE_DRIVER=opengauss+asyncpg
SEC_DATABASE_HOST=your-opengauss-host
SEC_DATABASE_PORT=5432
SEC_DATABASE_USERNAME=your-username
SEC_DATABASE_PASSWORD=your-password
SEC_DATABASE_NAME=your-database
SEC_DATABASE_SCHEMA=sec
```

使用前在项目根目录运行 `uv sync --extra opengauss` 安装可选的 SQLAlchemy 方言和异步驱动。密码可直接填写 `@` 等特殊字符，无需 URL 编码。设置这些字段时应清空 `SEC_DATABASE_URL`。该 extra 使用 `opengauss-sqlalchemy` 与 `asyncpg`，项目将 SQLAlchemy 约束在兼容的 2.0.x 版本范围；项目尚未对目标 openGauss 服务做集成验证。

`SEC_DATABASE_SCHEMA` 让每条连接使用该 schema 的 `search_path`，无需改数据库用户的全局默认值。先由管理员创建 schema 并授权，例如 `CREATE SCHEMA sec AUTHORIZATION sec_app;`，将 `sec_app` 替换为 `SEC_DATABASE_USERNAME`。若 schema 已存在，则授予应用用户 `USAGE` 和 `CREATE` 权限。

CLI 启动后会显示 Session ID。重启后通过以下方式恢复：

```sh
uv run python -m sec --session <session-id>
```

SDK 也可传入 `session_id`：

```python
async with AsyncSecClient(
    "assistant",
    "遵循用户指令。",
    session_id="existing-session-id",
) as client:
    result = await client.run("继续之前的工作").result()
```

## 自定义生命周期

高级应用可创建并初始化共享 Database，再创建 SessionStore 并恢复 Session。SessionStore 不拥有调用方注入的 Database；应用应在关闭时关闭 Database。对应 API 为 `create_database`、`create_session_store` 和 `Session.restore`，从 `sec.apis` 导入。

## 便携式备份

CLI 的 `/export [path]` 和 `/import <path>` 可在不同 SessionStore 之间转移会话。导入会创建新 Session。恢复时若历史中有未提交结果的工具调用，Sec 会记录结果未知，不会自动重放工具；这避免因不确定的副作用而重复执行。

SessionStore 的原子追加和乐观并发契约见[存储适配器教程](14-implement-storage-adapters.md)。

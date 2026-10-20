# 管理 Sessions

Session 保存一段对话历史。CLI 启动时会创建新 Session，也可通过 `--session <id>` 恢复已有 Session。SDK 也可在构造 Client 时传入 `session_id`。

## CLI 操作

- `/session` 查看当前 Session 信息。
- `/name <name>` 为当前 Session 命名。
- `/tree` 查看会话分支。
- `/fork` 复制当前活动分支到新 Session。
- `/clone` 复制完整分支树到新 Session。
- `/new` 创建新 Session；`/resume [session-id]` 恢复已有 Session。

切换、fork 或 clone 前必须先结束活动 Trace。

## 导入与导出

```text
/export
/export ./backup.json
/import ./backup.json
```

默认导出到 `.sec/sessions/`，不会覆盖已有文件。导入会校验文件并创建新 Session，不会覆盖当前会话。

## 持久化边界

SessionStore 配置为 `in_memory` 时，历史只保存在当前进程中；重启后不能用 Session ID 恢复。要跨进程恢复，请配置 SQLite 数据库，见[持久化和迁移 Sessions](10-persist-and-transfer-sessions.md)。

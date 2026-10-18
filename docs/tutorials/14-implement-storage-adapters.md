# 实现存储适配器

一般应用先使用内置 SessionStore 即可。本教程面向需要自定义持久化后端的开发者。

## SessionStore

`SessionStoreProtocol` 要求实现异步 `load(session_id)`、`list_sessions(limit)` 和 `append(...)`。`append` 使用 `expected_version` 做乐观并发控制，必须原子写入整个条目批次及活动叶选择；失败不能留下部分写入。

数据库 SessionStore 由 `Database` 提供连接。若应用创建并注入 Database，则应用负责初始化和关闭它；SessionStore 不会关闭调用方传入的 Database。使用内置 SQLite 时，可参照[Session 持久化教程](10-persist-and-transfer-sessions.md)。

恢复 Session 可使用 `Session.restore(store, session_id)`。如果工具副作用已发生但工具结果未持久化，恢复逻辑不会自动重放该工具；存储失败后应重新读取状态核对，不能盲目重试追加。

Extension 也可以通过 `register_storage_backend(name, factory)` 注册 SessionStore 后端。

## ObjectStore

`ObjectStore` 是 `sec.apis` 导出的异步协议，要求实现：

- `put(key, data: bytes, content_type=...)`
- `get(key)`，返回 `StoredObject`；对象缺失时抛出 `ObjectNotFoundError`
- `delete(key)`；删除不存在对象应视为成功
- `aclose()`

相同 key 的 `put` 覆盖旧对象。当前接口以 `bytes` 全量读写对象，没有流式大对象接口，也没有内置云厂商实现。适配器需要明确处理关闭后的调用和客户端资源生命周期；注入到应用的适配器由应用负责关闭。

## 验证建议

为自定义实现覆盖版本冲突、原子追加、缺失 Session、无效输入和资源关闭。集成测试应使用临时数据库或临时对象空间，避免依赖生产凭据和外部服务。

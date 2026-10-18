# 配置参数参考

Sec 从 `.env`、进程环境变量和 Client 显式参数读取配置。本文列出项目根目录 [`.env.example`](../../.env.example) 中的全部变量，说明默认值、有效范围和相互依赖。

## 配置优先级

总体优先级从高到低为：

1. `SecClient` / `AsyncSecClient` 构造时传入的显式参数。
2. 进程环境变量。
3. Client 配置读取的 `.env` 文件。
4. Sec 代码默认值。

Provider 的每项设置会先在当前来源查找通用变量，再查找所选 Provider 的专属变量。例如选择 `openai` 时，模型依次查找 `SEC_MODEL`、`OPENAI_MODEL`；API key 和 Base URL 也遵循同样顺序。环境中的 Provider 专属值优先于 `.env` 中的通用值。

空字符串会按未设置处理。不要将 `.env` 提交到仓库；其中可能包含凭据和数据库 URL。

## Provider 与凭据

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEC_PROVIDER` | `openai` | 内置值为 `openai`、`anthropic`；也可选择已启用 Extension 注册的 Provider。 |
| `SEC_MODEL` | 无 | 通用模型名；优先于当前来源的 Provider 专属模型名。必须设置一个有效模型名。 |
| `SEC_API_KEY` | 无 | 通用 API key；优先于当前来源的 Provider 专属 key。必须设置。 |
| `SEC_BASE_URL` | 无 | 通用 API endpoint；同一来源中优先于 Provider 专属 Base URL。 |
| `OPENAI_MODEL` | 无 | `SEC_PROVIDER=openai` 时的模型名。 |
| `OPENAI_API_KEY` | 无 | OpenAI 凭据；若没有 `SEC_API_KEY`，此项必填。 |
| `OPENAI_BASE_URL` | 无 | OpenAI 兼容端点或代理地址。 |
| `ANTHROPIC_MODEL` | 无 | `SEC_PROVIDER=anthropic` 时的模型名。 |
| `ANTHROPIC_API_KEY` | 无 | Anthropic 凭据；若没有 `SEC_API_KEY`，此项必填。 |
| `ANTHROPIC_BASE_URL` | 无 | Anthropic 兼容端点或代理地址。 |

只需填写所选 Provider 对应的一组专属变量，或使用 `SEC_MODEL` / `SEC_API_KEY` / `SEC_BASE_URL` 通用变量。当前内置 Provider 支持文本和工具交互，不支持多模态内容。

Extension Provider 的变量前缀由 Provider 名称生成：转换为大写，并将 `-` 替换为 `_`。例如 Provider 名称 `my-provider` 对应 `MY_PROVIDER_MODEL`、`MY_PROVIDER_API_KEY` 和 `MY_PROVIDER_BASE_URL`。通用 `SEC_*` 变量仍可覆盖这些值。

## 请求与输出

| 变量 | 默认值 | 有效范围与说明 |
|---|---:|---|
| `SEC_REQUEST_TIMEOUT_SECONDS` | `60` | 正有限数值，单位秒。 |
| `SEC_MAX_OUTPUT_TOKENS` | `4096` | 大于等于 1 的整数。 |

对应 Client 显式参数为 `request_timeout_seconds` 和 `max_output_tokens`。

## 模型请求重试

| 变量 | 默认值 | 有效范围与说明 |
|---|---:|---|
| `SEC_MODEL_MAX_ATTEMPTS` | `3` | 大于等于 1 的整数，包含首次尝试。 |
| `SEC_MODEL_RETRY_BASE_DELAY_SECONDS` | `0.5` | 非负有限数值，单位秒。 |
| `SEC_MODEL_RETRY_MAX_DELAY_SECONDS` | `8` | 非负有限数值，单位秒；必须不小于 base delay。 |

模型重试仅用于可重试的连接错误、超时、限流和服务端错误。`model_calls` 统计逻辑请求，`model_attempts` 和 `retries` 统计实际请求尝试及重试次数。对应 Client 参数为 `max_model_attempts`、`model_retry_base_delay_seconds` 和 `model_retry_max_delay_seconds`。

## 工具重试

| 变量 | 默认值 | 有效范围与说明 |
|---|---:|---|
| `SEC_TOOL_MAX_ATTEMPTS` | `1` | 大于等于 1 的整数，包含首次尝试。 |
| `SEC_TOOL_RETRY_BASE_DELAY_SECONDS` | `0.5` | 非负有限数值，单位秒。 |
| `SEC_TOOL_RETRY_MAX_DELAY_SECONDS` | `8` | 非负有限数值，单位秒；必须不小于 base delay。 |

默认不重试工具。只有工具通过 `@tool(retry_safe=True)` 显式声明可安全重试，且调用抛出 `RetryableToolError`，才会按此配置重试。不要为可能产生重复副作用的操作启用安全重试。对应 Client 参数为 `max_tool_attempts`、`tool_retry_base_delay_seconds` 和 `tool_retry_max_delay_seconds`。

工具参数校验失败会作为错误结果交给模型；该调用尚未执行，模型可用新的工具调用 ID 提交修正参数。Executor 可抛出 `RecoverableToolError`，将其安全、面向模型的消息作为错误结果返回；消息最多 2000 个字符，调用方负责避免包含凭据或其他敏感信息。运行时不会自动重放这类调用，并会提示模型副作用状态可能不确定。

安全重试耗尽后，运行时将通用错误结果交给模型，不转发 Executor 的异常详情。未声明 `retry_safe=True` 的工具遇到 `RetryableToolError`、普通 Executor 异常及 Executor 协议错误仍会终止 Run；审批拒绝仍作为错误结果返回，取消仍终止 Run。结果的 `metadata["error_kind"]` 是 sec 内部分类，不会作为 Provider 工具结果内容发送。

## Agent 预算与事件缓冲

| 变量 | 默认值 | 有效范围与说明 |
|---|---:|---|
| `SEC_AGENT_MAX_MODEL_CALLS` | `8` | 每个 turn 的最大逻辑模型调用数，正整数。 |
| `SEC_AGENT_MAX_TOOL_CALLS` | `32` | 每个 turn 的最大工具调用数，正整数。 |
| `SEC_EVENT_BUFFER_SIZE` | `128` | Run 事件历史及订阅缓冲的容量，正整数；单位是事件条数，不是字节数。 |

新的 follow-up turn 会重置该 turn 的调用预算。事件缓冲是有限历史，不是持久化审计日志。对应 Client 参数为 `max_model_calls`、`max_tool_calls` 和 `event_buffer_size`。

## 上下文压缩

| 变量 | 默认值 | 有效范围与说明 |
|---|---:|---|
| `SEC_CONTEXT_WINDOW_TOKENS` | 未设置 | 可选正整数；未设置时上下文压缩关闭。 |
| `SEC_CONTEXT_COMPRESSION_THRESHOLD` | `0.8` | `(0, 1]` 范围内的比例，在估算上下文达到该比例时触发压缩。 |

仅设置窗口大小不足以启用压缩：Client 还必须注入匹配 Provider 请求格式的 `TokenEstimator`。设置了窗口但没有估算器会导致初始化报错；SDK 不会按字符数猜测 token 数。对应参数为 `context_window_tokens`、`compression_threshold` 和 `token_estimator`。

## Session 存储

| 变量 | 代码默认值 | 有效范围与说明 |
|---|---|---|
| `SEC_SESSION_STORE_BACKEND` | `in_memory` | `in_memory`、`database`，或已启用 Extension 注册的存储后端名。 |
| `SEC_DATABASE_URL` | 未设置 | SQLAlchemy 异步数据库 URL；与下方拆分连接字段二选一。 |
| `SEC_DATABASE_DRIVER` | 未设置 | SQLAlchemy 方言和驱动名，例如 `opengauss+asyncpg`。 |
| `SEC_DATABASE_HOST` | 未设置 | 数据库主机名或 IP。 |
| `SEC_DATABASE_PORT` | 未设置 | 整数，范围为 `1`–`65535`。 |
| `SEC_DATABASE_USERNAME` | 未设置 | 数据库用户名。 |
| `SEC_DATABASE_PASSWORD` | 未设置 | 数据库密码；原样传递并由 SQLAlchemy 安全编码，不需手工转换 URL 特殊字符。 |
| `SEC_DATABASE_NAME` | 未设置 | 数据库名。 |
| `SEC_DATABASE_SCHEMA` | 未设置 | openGauss schema；通过连接级 `search_path` 选择，不修改用户的全局默认值。当前仅支持 `opengauss+asyncpg`，名称须为简单 SQL 标识符。 |

项目 [`.env.example`](../../.env.example) 提供三种配置范例。根据需要只启用其中一种：

仅在当前进程中保存（进程退出后无法恢复）：

```dotenv
SEC_SESSION_STORE_BACKEND=in_memory
SEC_DATABASE_URL=
```

本地 SQLite（模板默认示例）：

```dotenv
SEC_SESSION_STORE_BACKEND=database
SEC_DATABASE_URL=sqlite+aiosqlite:///sec.db
```

openGauss 示例（密码可直接写 `@` 等特殊字符，不要将其嵌入 URL）：

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

在项目根目录运行 `uv sync --extra opengauss` 安装可选的 SQLAlchemy 方言和异步驱动。此 extra 使用 `opengauss-sqlalchemy` 与 `asyncpg`；项目将 SQLAlchemy 约束在兼容的 2.0.x 版本范围。项目尚未对目标 openGauss 服务做集成验证。

使用拆分字段时必须完整设置 `SEC_DATABASE_DRIVER`、`SEC_DATABASE_HOST`、`SEC_DATABASE_PORT`、`SEC_DATABASE_USERNAME`、`SEC_DATABASE_PASSWORD` 和 `SEC_DATABASE_NAME`，并清空 `SEC_DATABASE_URL`。拆分字段与 `SEC_DATABASE_URL` 不能混用。`in_memory` 后端不能配置任何数据库连接参数。切换到 SQLite 以外的数据库时，必须安装对应的 SQLAlchemy 异步驱动和方言。

使用 `SEC_DATABASE_SCHEMA` 前，需先在目标数据库创建 schema 并授予应用用户权限，例如由管理员执行 `CREATE SCHEMA sec AUTHORIZATION sec_app;`，其中 `sec_app` 替换为 `SEC_DATABASE_USERNAME`。若 schema 已存在，则授予该用户 `USAGE` 和 `CREATE` 权限。Sec 会在每个 openGauss 连接上设置 `search_path`，不会在 `public` 中创建 Session 表，也不会改变该用户的全局 `search_path`。

`.env` 中密码含有 `#` 或空白等 dotenv 语法字符时，可将整个值用引号包裹，例如 `SEC_DATABASE_PASSWORD='p@ss#word'`。密码仍是原始密码，不需要 URL 编码。

## Client 参数与 CLI 参数

Client 的显式参数名称与环境变量大致一一对应，例如 `SEC_REQUEST_TIMEOUT_SECONDS` 对应 `request_timeout_seconds`，`SEC_DATABASE_URL` 对应 `database_url`。显式传入 `database_url` 时会覆盖环境中的拆分连接字段。CLI 默认从工作目录的 `.env` 读取配置，不会自动加载 `.env.local`；SDK 可通过 `env_file` 指定 dotenv 文件。使用 SDK 时见[同步 Client 教程](../tutorials/07-build-a-sync-client.md)和[异步与流式 Client 教程](../tutorials/08-build-an-async-streaming-client.md)。

CLI 的 `--mode`、`--session` 和 `--extension` 是启动参数，不是 `.env` 配置变量。CLI 权限模式和启动选项见[终端 CLI 教程](../tutorials/03-use-the-cli.md)。

## 参数校验

Sec 会在加载配置时校验数值格式、正负范围及依赖关系；配置缺失或无效时会抛出 `ConfigError`，错误消息指出变量名或约束，不包含 API key 等配置值。修改配置后重启 Client/CLI，使新值生效。

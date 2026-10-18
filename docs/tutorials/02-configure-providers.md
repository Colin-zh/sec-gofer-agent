# 配置模型 Provider

Sec 当前内置 OpenAI 与 Anthropic Provider，支持文本响应和工具调用。多模态内容尚未适配；收到不支持的内容时会明确报错。

## 使用 `.env`

项目根目录的 `.env.example` 是完整配置清单。复制后按需填写：

```dotenv
SEC_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_MODEL=...
```

切换到 Anthropic 时，将 `SEC_PROVIDER` 设为 `anthropic`，填写 `ANTHROPIC_API_KEY` 和 `ANTHROPIC_MODEL`。需要代理或兼容端点时，可设置对应 Provider 的 `*_BASE_URL`。

配置优先级从高到低为：Client 显式参数、进程环境变量、`.env`、代码默认值。在同一来源内，通用项 `SEC_MODEL`、`SEC_API_KEY`、`SEC_BASE_URL` 优先于 Provider 专属项。完整变量、默认值和校验范围见[配置参数参考](../reference/configuration.md)。

## 在代码中显式配置

```python
from sec.apis import AsyncSecClient

async with AsyncSecClient(
    "assistant",
    "遵循用户指令。",
    provider="openai",
    model="your-model",
    api_key="从安全配置中读取",
) as client:
    result = await client.run("你好").result()
    print(result.text)
```

不要把示例中的占位文本替换成硬编码密钥并提交。生产应用应从环境变量或密钥管理服务读取凭据。

## 请求与重试配置

可通过 Client 参数或 `.env` 调整请求超时、输出 token 上限、模型重试次数、工具重试和每轮调用预算。变量默认值见项目根目录的 [`.env.example`](../../.env.example)；运行行为见[重试、预算与上下文压缩教程](13-retries-budgets-and-context-compaction.md)。

# 安装并完成第一次对话

## 前置条件

- macOS、Linux 或 Windows 上可用的 Python 3.12。
- [uv](https://docs.astral.sh/uv/)。
- OpenAI 或 Anthropic 的 API 凭据。

## 安装项目环境

在 Sec 项目根目录运行：

```sh
uv python install 3.12
uv sync
cp .env.example .env
```

打开 `.env`，填写 `SEC_PROVIDER`、对应的模型名和 API key。OpenAI 可填写 `OPENAI_MODEL`、`OPENAI_API_KEY`；Anthropic 可填写 `ANTHROPIC_MODEL`、`ANTHROPIC_API_KEY`。也可使用通用的 `SEC_MODEL` 和 `SEC_API_KEY` 覆盖 Provider 专属值。

真实密钥只保存在本地 `.env` 或安全的环境变量中，不要提交到 Git，也不要把密钥写入源代码或聊天 prompt。

## 启动聊天

```sh
uv run python -m sec
```

向 Sec 输入消息即可开始对话。输入 `/help` 查看命令，`Ctrl+O` 或 `/help full` 查看启动帮助和已加载资源。默认权限模式是 `normal`，有副作用的工具调用会请求批准。

## 下一步

- 了解 Provider 配置优先级：[配置模型 Provider](02-configure-providers.md)。
- 学习 CLI 命令和权限模式：[使用终端 CLI](03-use-the-cli.md)。

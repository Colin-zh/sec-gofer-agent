# Sec 教程

本目录按实际任务组织，示例默认从 Sec 项目根目录运行。需要先安装 Python 3.12、uv 并执行 `uv sync`；凡需访问模型的例子，还需要有效的 Provider 凭据。

所有 `.env.example` 参数的完整列表、默认值、校验范围和优先级见[配置参数参考](../reference/configuration.md)。

## 学习路线

### 我想先使用 Sec

1. [安装并完成第一次对话](01-install-and-first-chat.md)
2. [配置模型 Provider](02-configure-providers.md)
3. [使用终端 CLI](03-use-the-cli.md)
4. [管理 Sessions](04-manage-sessions.md)
5. [添加 Skills 和 Prompts](05-create-skills-and-prompts.md)
6. [理解工具、权限与审批](06-tools-permissions-and-approvals.md)

### 我想在 Python 应用中使用 Sec

1. [构建同步 Client](07-build-a-sync-client.md)
2. [构建异步与流式 Client](08-build-an-async-streaming-client.md)
3. [控制 Run 并处理事件](09-control-runs-and-consume-events.md)
4. [持久化和迁移 Sessions](10-persist-and-transfer-sessions.md)

### 我想扩展 Sec 或调优运行行为

1. [创建第一个 Extension](11-create-your-first-extension.md)
2. [注册工具及其他 Extension 能力](12-extend-sec-with-tools-and-hooks.md)
3. [配置重试、预算与上下文压缩](13-retries-budgets-and-context-compaction.md)
4. [实现存储适配器](14-implement-storage-adapters.md)
5. [排查常见问题](15-troubleshooting.md)

## 当前实现边界

教程只指导当前可用的功能。MCP 连接管理、Dashboard 和云厂商 ObjectStore 适配器尚未实现；上下文压缩需要同时提供窗口大小和匹配 Provider 的 `TokenEstimator`；内置 Bash 不是安全沙箱。每篇相关教程会说明这些前提和限制。

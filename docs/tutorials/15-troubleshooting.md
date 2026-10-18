# 排查常见问题

## Provider 配置错误

检查 `.env` 中的 Provider、模型名和 API key 是否匹配；显式 Client 参数和进程环境变量会覆盖 `.env`。不要把真实密钥贴进日志或提交到仓库。配置变量见[Provider 教程](02-configure-providers.md)和 [`.env.example`](../../.env.example)。

## Skill 或 Prompt 未加载

运行 `/help full` 查看加载问题，修改文件后执行 `/reload`。检查文件扩展名、目录位置、YAML frontmatter、资源正文是否为空，以及 `name` 是否为小写 kebab-case。若重命名文件但 frontmatter 仍有 `name`，以 frontmatter 为准。

工作区 `.sec/` 中的同名资源会覆盖 `~/.sec/` 中的用户资源；覆盖本身会显示为资源问题。

## Extension 显示已安装，但启用时报模块找不到

确认 Extension 安装在运行 `sec` 的同一个 Python 环境，entry point 名称与 Extension 对象的 `name` 完全一致，并且 entry point 的模块路径指向可导入模块。

macOS 的部分 Python 会跳过带 `UF_HIDDEN` 标记的 `.pth` 文件。若 editable 安装后出现 `No module named <extension_module>`，可检查：

```sh
ls -lO .venv/lib/python3.12/site-packages/<extension-name>.pth
```

如果输出包含 `hidden`，可移除该文件的隐藏标记后重新启动 Python：

```sh
chflags nohidden .venv/lib/python3.12/site-packages/<extension-name>.pth
```

也可非 editable 安装 Extension wheel，避免依赖 `.pth` 路径文件；源码修改后需要重新安装。

桌面应用已归入 `sec/apps/desktop/`，通过主包的 `desktop` 可选依赖安装，与 CLI 共用仓库根目录的 `.venv`。旧独立桌面环境不再使用，Extension 也应安装到主环境。例如：

```sh
uv sync --extra desktop --no-editable
uv pip install --python .venv/bin/python ./path/to/extension
uv run --no-sync sec-desktop --extension <extension-name>
```

`--no-editable` 避免 SEC 使用 editable `.pth` 文件；`--no-sync` 避免启动时移除未声明为项目依赖的本地 Extension。若依赖声明有变化，先重新执行 `uv sync --extra desktop --no-editable`，再安装本地 Extension。未安装 Qt 时，`sec-desktop` 会提示安装 `sec-gofer-agent[desktop]`，CLI 和 SDK 不受影响。

若桌面启动报告 Qt 找不到 `cocoa` 平台插件，先检查插件是否被 macOS 标记为隐藏。插件文件即使存在且可直接加载，Qt 默认也不会发现带 `hidden` 属性的插件。新版桌面入口会在创建窗口前清理其 Qt 插件目录内的隐藏标记、保留其他属性，并记录修复日志；权限不足会明确报错。旧安装可以只更新桌面包，不移除本地 Extension：

```sh
uv pip install --python .venv/bin/python --reinstall --no-deps .
uv run --no-sync sec-desktop
```

若仍激活着旧独立桌面环境，先退出该环境，或使用 `env -u VIRTUAL_ENV uv run --no-sync sec-desktop`。不匹配的 `VIRTUAL_ENV` 警告不是 Qt 加载失败的原因。

如果插件文件确实缺失或损坏，再强制重装 Qt wheel：

```sh
uv sync --extra desktop --no-editable \
  --reinstall-package pyside6 \
  --reinstall-package pyside6-essentials \
  --reinstall-package pyside6-addons
```

这会同步依赖并可能移除未声明的本地 Extension；之后应重新安装 Extension，再用 `uv run --no-sync sec-desktop` 启动。

## Session 无法跨进程恢复

检查 SessionStore 是否配置为 `database`，并确认 `SEC_DATABASE_URL` 指向同一个数据库。`in_memory` 后端只在当前进程有效。详情见[Session 持久化教程](10-persist-and-transfer-sessions.md)。

## 工具没有执行

检查当前权限模式、工具风险元数据和审批结果。管道式 CLI 没有交互审批界面，需要确认的工具会被拒绝。`auto` 不会自动批准 Bash；本地执行器也不是安全沙箱。

## Run 事件订阅溢出

事件订阅有界；如果消费速度跟不上事件发布，会抛出 `SecSubscriptionOverflow`。及时迭代事件，或按需增大 `event_buffer_size`。事件历史不是持久化审计日志。

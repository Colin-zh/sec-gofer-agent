# 创建第一个 Extension

Extension 是安装在 Sec 当前 Python 环境中的 Python 包。安装后仍须显式启用；它与只提供 Markdown 指令的 Skill/Prompt 不同，Extension 会执行 Python 代码。

## 创建包

```text
sec-hello-extension/
├── pyproject.toml
└── sec_hello/
    └── __init__.py
```

`pyproject.toml` 声明 entry point。entry point 名称必须与 Extension 对象的 `name` 一致：

```toml
[build-system]
requires = ["uv_build>=0.12.21,<0.13.0"]
build-backend = "uv_build"

[project]
name = "sec-hello-extension"
version = "0.1.0"
requires-python = ">=3.12"

[project.entry-points."sec.extensions"]
hello = "sec_hello:extension"

[tool.uv.build-backend]
module-name = "sec_hello"
module-root = "."
```

若将扩展发布到独立环境，请在 `[project]` 下声明与宿主兼容的 `sec-gofer-agent` 依赖版本。本地跟随 Sec 项目开发时，确保扩展安装到运行 Sec 的同一环境。

实现 Extension 并注册一个 CLI 命令：

```python
from sec.apps.cli.slash_commands import SlashCommand


def hello(args: tuple[str, ...]) -> int:
    print("Hello from extension!", *args)
    return 0


class HelloExtension:
    name = "hello"

    async def activate(self, api) -> None:
        api.register_command(SlashCommand("hello", "Say hello.", hello))

    async def deactivate(self) -> None:
        pass


extension = HelloExtension()
```

`activate(api)` 注册本次 Client 所需的能力；`deactivate()` 释放扩展拥有的资源。只有成功加载的 Extension 才会激活，关闭时按反向顺序停用。

## 安装并启用

将包安装到运行 Sec 的同一 Python 环境，然后从 Sec 项目根目录启用：

```sh
uv pip install --python .venv/bin/python /path/to/sec-hello-extension
uv run python -m sec --extension hello
```

聊天中输入 `/hello world`。也可运行一次性命令：

```sh
uv run python -m sec --extension hello /hello world
```

Extension 不会因安装而自动执行；每次启动都必须通过 `--extension` 或 SDK 的 `enabled_extensions=("hello",)` 显式启用。当前 Client 不支持 Extension 热重载。

> macOS 上 editable 安装若报 `No module named sec_hello`，先查看[排查教程](15-troubleshooting.md)中的 `.pth` 隐藏标记说明。

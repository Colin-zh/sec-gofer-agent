"""验证已构建并安装的 Python 包可被 sec.extensions 发现和启用。"""

from __future__ import annotations

import asyncio
import importlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sec.apis import AsyncSecClient


class InstalledExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_entry_point_is_loaded_and_deactivated(self) -> None:
        uv = shutil.which("uv")
        if uv is None:
            self.fail("uv is required to build and install the extension smoke package")

        with tempfile.TemporaryDirectory(prefix="sec-extension-smoke-") as temp_dir:
            root = Path(temp_dir)
            package_dir = root / "source"
            package_dir.mkdir()
            wheel_dir = root / "wheels"
            install_dir = root / "installed"
            extension_name = "sec-smoke-extension"
            module_name = "sec_smoke_extension"
            module_dir = package_dir / module_name
            module_dir.mkdir()

            (package_dir / "pyproject.toml").write_text(
                "\n".join(
                    (
                        "[build-system]",
                        'requires = ["uv_build>=0.12.21,<0.13.0"]',
                        'build-backend = "uv_build"',
                        "",
                        "[project]",
                        f'name = "{extension_name}"',
                        'version = "0.0.1"',
                        'requires-python = ">=3.12"',
                        "",
                        '[project.entry-points."sec.extensions"]',
                        'installed-smoke = "sec_smoke_extension:extension"',
                        "",
                        "[tool.uv.build-backend]",
                        f'module-name = "{module_name}"',
                        'module-root = "."',
                        "",
                    )
                ),
                encoding="utf-8",
            )
            (module_dir / "__init__.py").write_text(
                "\n".join(
                    (
                        "from sec.tools import tool",
                        "",
                        "class SmokeExtension:",
                        '    name = "installed-smoke"',
                        "",
                        "    def __init__(self):",
                        "        self.lifecycle = []",
                        "",
                        "    async def activate(self, api):",
                        '        self.lifecycle.append("activate")',
                        "        api.register_tool(",
                        '            tool(lambda: "ok", name="installed-smoke-tool",',
                        '                 description="Installed smoke tool.")',
                        "        )",
                        "",
                        "    async def deactivate(self):",
                        '        self.lifecycle.append("deactivate")',
                        "",
                        "extension = SmokeExtension()",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    uv,
                    "build",
                    "--project",
                    str(package_dir),
                    "--out-dir",
                    str(wheel_dir),
                    "--wheel",
                ],
                check=True,
            )
            wheel = next(wheel_dir.glob("*.whl"))
            subprocess.run(
                [
                    uv,
                    "pip",
                    "install",
                    "--target",
                    str(install_dir),
                    "--no-deps",
                    str(wheel),
                ],
                check=True,
            )

            original_path = sys.path.copy()
            sys.path.insert(0, str(install_dir))
            try:
                async with AsyncSecClient(
                    "smoke",
                    "Smoke test.",
                    provider="openai",
                    model="smoke-model",
                    api_key="not-used",
                    enabled_extensions=("installed-smoke",),
                    env_file=None,
                ) as client:
                    self.assertEqual(
                        tuple(tool.definition.name for tool in client.agent.tools),
                        ("installed-smoke-tool",),
                    )
                extension_module = importlib.import_module(module_name)
                self.assertEqual(
                    extension_module.extension.lifecycle,
                    ["activate", "deactivate"],
                )
            finally:
                sys.path[:] = original_path
                sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()

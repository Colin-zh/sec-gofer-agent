from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

from sec.apps.desktop import main


class DesktopEntryTests(unittest.TestCase):
    def test_missing_extra_reports_installation_without_importing_gui(self) -> None:
        output = io.StringIO()
        with (
            patch("sec.apps.desktop.find_spec", return_value=None),
            redirect_stderr(output),
        ):
            self.assertEqual(main(), 1)
        self.assertIn("pip install 'sec-gofer-agent[desktop]'", output.getvalue())
        self.assertIn("uv sync --extra desktop", output.getvalue())

    def test_core_and_desktop_entry_import_without_qt(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.abc, sys\n"
                "class NoQt(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, fullname, path=None, target=None):\n"
                "        if fullname.split('.')[0] in {'PySide6', 'shiboken6'}:\n"
                "            raise ImportError('Qt must not be imported')\n"
                "sys.meta_path.insert(0, NoQt())\n"
                "import sec, sec.apps, sec.apps.cli, sec.apps.desktop\n"
                "assert not any(name.startswith('PySide6') for name in sys.modules)\n",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_qt_dependency_is_only_in_desktop_extra(self) -> None:
        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )["project"]
        self.assertFalse(
            any("pyside6" in dependency.lower() for dependency in project["dependencies"])
        )
        self.assertIn("PySide6>=6.8.0", project["optional-dependencies"]["desktop"])
        self.assertEqual(project["scripts"]["sec-desktop"], "sec.apps.desktop:main")

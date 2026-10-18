"""Desktop tests are optional, like the GUI dependency itself."""

from importlib.util import find_spec
import unittest

if find_spec("PySide6") is None:
    raise unittest.SkipTest("Install sec-gofer-agent[desktop] to run desktop tests")

from sec.apps.desktop.qt_plugins import prepare_qt_plugins

prepare_qt_plugins()

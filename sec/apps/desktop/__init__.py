"""Optional desktop UI for Sec."""

from importlib.util import find_spec
import sys


def main() -> int:
    """Load the GUI only when the desktop entry point is invoked."""
    if find_spec("PySide6") is None:
        print(
            "Sec Desktop requires optional dependencies. "
            "Install with: pip install 'sec-gofer-agent[desktop]' "
            "(repository: uv sync --extra desktop --no-editable)",
            file=sys.stderr,
        )
        return 1
    from sec.apps.desktop.app import main as desktop_main

    return desktop_main()

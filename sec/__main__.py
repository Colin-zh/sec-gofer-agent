"""将 ``python -m sec`` 转发到正式 CLI 应用入口。"""

from sec.apps.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

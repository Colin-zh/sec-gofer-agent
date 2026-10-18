# Sec Desktop

Sec Desktop is an optional PySide6 UI hosted in `sec.apps.desktop`. The SDK,
CLI, and desktop share the same distribution and environment. The distribution
name is `sec-gofer-agent`; the Python import and CLI command remain `sec`.

## Install and run

Default installation does not install Qt:

```sh
pip install sec-gofer-agent
```

To enable the desktop UI:

```sh
pip install 'sec-gofer-agent[desktop]'
sec-desktop
# Equivalent module entry point:
python -m sec.apps.desktop
```

From the repository root:

```sh
uv sync --extra desktop --no-editable
uv run --no-sync sec-desktop
```

The non-editable install avoids a macOS Python issue where hidden editable
`.pth` files may be ignored. After source changes, refresh the installed package:

```sh
uv pip install --python .venv/bin/python --reinstall --no-deps .
```

The former standalone `sec-desktop` project and `sec_desktop` import have been
replaced by this application. Its old virtual environment is no longer used.
Configure the provider with the repository root `.env` or `SEC_*` variables.
The app uses the current working directory as its workspace.

## Extensions and commands

Install extensions into the same environment as SEC:

```sh
uv pip install --python .venv/bin/python ./extensions/sec-hello-extension
uv run --no-sync sec-desktop --extension hello
```

`--extension` is repeatable. `--no-sync` preserves local extensions that are not
declared project dependencies. After syncing again, reinstall local extensions.

The desktop reuses the CLI slash-command registry, including `/help`, `/new`,
`/resume`, `/name`, `/session`, `/tree`, `/fork`, `/clone`, `/mode`, `/trust`,
`/reload`, `/export`, `/import`, `/copy`, `/stop`, `/steer`, `/followup`, and
commands contributed by enabled extensions. Type `/` for completion.

## Window modes

- **大屏专注**: full chat UI with recent Sessions, streaming responses, command
  completion, and optional always-on-top behavior.
- **悬浮球**: click “切换悬浮球” to collapse the chat to a draggable, always-on-top
  icon. Click it to restore; right-click to restore or quit.
- Switching preserves the Session, unsent input, window geometry, and active
  Run. The ball's position is remembered; startup uses focus mode.
- Tool approvals and runtime errors restore the chat so they cannot stay hidden.

The icon is bundled at `sec/apps/desktop/assets/icon.png`, copied from
`docs/images/icon.png`. The floating ball is currently a launcher, not an inline
chat panel. System tray integration, global shortcuts, and native installers
are not included yet.

Built-in workspace and Bash tools run with the current user's operating-system
permissions, not in a security sandbox. Only use trusted workspaces/extensions.
Tool approval remains available in the UI.

## macOS Qt plugins

Qt skips plugin paths marked with the macOS filesystem `hidden` flag, even when
the Cocoa library is present and loadable. The desktop entry point clears that
flag within its installed Qt plugin directory before creating the application,
preserves other flags, and logs repairs. Permission failures are explicit.

If plugin files are actually missing or damaged:

```sh
uv sync --extra desktop --no-editable \
  --reinstall-package pyside6 \
  --reinstall-package pyside6-essentials \
  --reinstall-package pyside6-addons
```

Reinstall any local extensions removed by sync before launching with `--no-sync`.

## Tests

Core-only environments skip the desktop test package. For headless GUI tests:

```sh
QT_QPA_PLATFORM=offscreen uv run --extra desktop --no-sync \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

# Sec Desktop

Sec Desktop is an optional PySide6 UI hosted in `sec.apps.desktop`. The SDK,
CLI, and desktop share the same distribution and environment. The distribution
name is `sec-gofer-agent`; the Python import and CLI command remain `sec`.

Empty Sessions, including newly created conversations, show the same welcome
panel with command help and resource reload shortcuts. Permission modes remain
available in the composer `mode` menu, not in the welcome suggestions.

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

When the configured Session database uses `opengauss+asyncpg`, retain both
optional dependency groups on every sync:

```sh
uv sync --extra desktop --extra opengauss --no-editable
uv run --no-sync sec-desktop
```

For pip installations, use `pip install 'sec-gofer-agent[desktop,opengauss]'`.
Syncing only the `desktop` extra removes the optional openGauss dialect and
`asyncpg` driver and prevents that database configuration from starting.

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
`/agents`, `/agent`, `/mcp`, `/plugins`, `/reload`, `/export`, `/import`, `/copy`,
`/stop`, `/steer`, `/followup`, and
commands contributed by enabled extensions. Type `/` for completion.

## Chat layout and controls

- User messages appear in right-aligned bubbles; Agent Markdown appears on the
  left with a transparent background. Both use black text, during streaming
  and after history restoration.
- The header displays the complete, selectable Session ID. Status dots use green
  for ready, amber for running, blue for completed, purple for approval, red
  for errors, and gray for connecting/stopped states; text labels remain visible.
  The dot and status text appear directly after the Session ID on the same row.
  The header's right side contains only the observation and compact controls.
  Click the conversation name above the Session ID to rename the current Session
  using the same dialog as the recent-session menu. Message refreshes preserve it.
- The sidebar has icon-assisted primary entries: 新对话、快捷指令、定时任务、记忆、工具.
  记忆 opens a placeholder page for episodic, semantic, and procedural memory;
  no memory read/write/delete operations are connected yet.
  定时任务 is a disabled placeholder. Tool categories are
  智能体、提示词模板、技能、插件、MCP. Agent identity uses core AgentCards;
  prompt templates remain explicit one-shot input resources.
  Composer controls appear as help, agent, mode, reload with equal spacing;
  agent opens the AgentCard selector after the runtime is ready.
  Selection preserves the Session; changes during a Run queue for the next Run.
  Controls use content-sized icon/text bounds without native menu padding.
  Reserved agent/timer icons retain their source colors while disabled.
  The icon to the left of the conversation title collapses/expands the sidebar,
  restoring its previous width without changing the conversation or draft.
  It is highlighted while the sidebar is collapsed, matching the pin button's
  active highlight. Both icon-only toggles keep their icons centered.
  快捷指令, the composer help button, and `/help` open a dedicated searchable
  command-reference page instead of adding help text to the conversation.
  Category cards reflect the current command completions, including enabled
  skills/prompts and extension commands. The apply icon (tooltip: 填入输入框) inserts a command for editing
  without executing it; 返回对话 preserves the conversation and draft.
  The conversation's left-side tick navigation previews user inputs on hover
  and jumps to them on click. When reading earlier messages, a 回到底部 button
  returns to the latest output and resumes automatic following.
  工具 starts collapsed; click its row (with a right-aligned more icon) to
  expand/collapse the indented, softly shaded submenu without leaving chat.
  Under 工具, text-only category entries show the available
  count (including disabled resources) on the lower right in gray.
  Selecting a category opens a preview and activation page; 返回对话 preserves
  the current Session, messages, draft, and running task.
- Skill/prompt activation immediately removes or restores its slash command and
  completion. Full text and source paths remain available for preview. These
  prompt bodies are expanded explicitly. Skill summaries are automatically
  available to the selected Agent, with native loading and required-trigger
  activation handled by the core runtime. Shared activation choices persist in
  `.sec/resources.json`; an existing `.sec/desktop-resources.json` is reused for
  backwards compatibility. CLI and Desktop use the same resource catalog.
- Extension activation reuses `/reload enable|disable <name>` and the SDK's live
  loading, command refresh, and rollback logic. Installed but inactive extensions
  remain visible without importing their code for preview. Desktop choices persist
  in `.sec/desktop-extensions.json`; explicit saved choices override `--extension`
  defaults on restart. Saving failures are reported and trigger runtime rollback.
  The plugin page also offers tools-only worker execution. Choose the mode while
  the plugin is disabled. Worker choices persist in `.sec/worker-extensions.json`;
  reload imports updated tool code in a fresh process without restarting the
  Session. In-process extensions retain complete contributions but require host
  restart for Python code changes. Neither mode is a security sandbox.
- The page's global permission control changes the real `/mode` policy:
  normal asks for every tool, plan permits only built-in workspace reads, and auto
  auto-approves workspace file operations while retaining shell/remote approval.
  Activation never grants blanket approval. Global permissions and in-process
  extensions require an idle Run; skills, prompts, MCP and worker tools can
  publish new generations at safe request boundaries during a Run.
  Permission mode is shared with chat commands and starts in normal on restart.
  Its selector and popup use the same light palette, rounded corners, and
  selection highlight as the rest of the desktop UI, with a muted disabled state.
- MCP displays configured enabled and disabled services from `.sec/mcp.json`.
  Core adapters support stdio and Streamable HTTP, schema-backed tools, resource
  reads and prompt retrieval through the existing policy/approval chain.
  Reload stages and validates a complete candidate before publication; failure
  leaves the previous generation active. Credentials are not shown in inventory.
  See the [AgentCard, MCP and hot-reload guide](../../../docs/tutorials/16-agents-capabilities-and-reload.md)
  for configuration and lifecycle boundaries.
- Recent Sessions display the name on the left (elided when necessary) and a
  smaller gray `YY-MM-DD HH:MM` timestamp on the right. The complete Session ID
  remains selectable on a single line in the header.
- Right-click a recent Session to open, rename, fork the active branch, clone
  the entire tree, export JSON, copy its ID, or delete it. Actions target the
  clicked Session, not the current one. Fork/clone open the new copy; rename
  and export leave the current Session unchanged. Delete requires confirmation;
  deleting the current Session opens a new empty one. Files are never silently
  overwritten during export.
  The context menu uses text-only options, a light rounded panel, and the same
  soft selection highlight as other desktop menus.
  Rename, delete confirmation, export, and error dialogs share the desktop's
  light palette, rounded controls, and consistent button styling. Export uses a
  non-native styled file picker; cancel/close never performs a destructive action.
- Session mutations are rejected while a Run is active. Built-in memory and
  database stores support version-checked deletion (database deletion is
  transactional). Third-party stores without deletion report an explicit error.
  The async Client exposes `rename_session(id, name)`, `delete_session(id)`,
  `get_session_snapshot(id)`, `fork_session(id)`, and `clone_session(id)`.
- The compact composer keeps icon-assisted `help`, `agent`, `mode`, and `reload`
  buttons. Its completion popup displays command names and descriptions but
  inserts only the command. The visible button labels omit `/`; the underlying
  slash commands remain supported.
- Send is a split control: the left submits using the selected mode, while the
  right menu selects 停止当前后发送 (default), steer, or followup. Default mode
  cancels an active Run and waits for its cleanup before starting the new turn
  in the same Session; idle sends start immediately. Steer/followup require an
  active Run and preserve the draft otherwise.
  The send and stop buttons are combined: while running, an empty input shows
  停止 with a white button group; entering text restores the selected send action
  and dark styling. When idle it shows the
  selected action. Shortcuts also select and check their corresponding mode:
  Command/Ctrl+Enter selects default send, +Shift+Enter selects steer, and
  +Alt+Enter selects followup, including when an empty draft is not submitted.
  The `mode` menu directly selects normal/plan/auto, reflects changes from the
  tool page or `/mode`, and is disabled while a Run is active. The button opens
  this menu on click without displaying a dropdown arrow.
- Desktop shortcuts (use Command on macOS, Ctrl elsewhere): Command/Ctrl+Enter
  sends, Command/Ctrl+Shift+Enter steers the active Run, and
  Command/Ctrl+Alt+Enter queues a follow-up. Enter alone inserts a newline.
  Steering/follow-up preserve quotes and multiline input. If no Run is active,
  they show a notice and preserve the draft. `/follow` and `/follow-up` are
  aliases of `/followup`. The CLI's existing Alt+S / Alt+F shortcuts are unchanged.
- New chat, commands, tools, pin, refresh, and send/stop
  use icons with visible labels or hover/accessibility
  descriptions.

## Window modes

- **大屏专注**: full chat UI with recent Sessions, streaming responses, command
  completion, and optional always-on-top behavior.
- **悬浮球**: click the circular icon (hover: “切换悬浮球”) to collapse the chat to a draggable, always-on-top
  icon. Click it to restore; right-click to restore or quit.
- Switching preserves the Session, unsent input, window geometry, and active
  Run. The ball's position is remembered; startup uses focus mode.
- Tool approvals and runtime errors restore the chat so they cannot stay hidden.
- Tool approval requests appear as in-conversation cards with 拒绝 / 允许本次
  actions instead of blocking popups. A choice disables both buttons immediately;
  it is saved before being submitted to the Run. Requests, decisions, and
  cancellations are stored as display-only Session entries, outside model context.
  Reopening a Session shows read-only records, never active approval buttons.
  Tool calls, arguments, and persisted results also appear in the conversation.
  Each interaction keeps the user input and final Markdown answer visible.
  Intermediate assistant messages, tool calls/results, and historical approvals
  are collected in a collapsed 处理过程 summary. Expand it to see smaller-text
  steps, each with its own disclosure control. Pending approvals remain visible
  and actionable outside the collapsed process. Expansion preserves the clicked
  row's viewport position; new content follows the bottom only when already
  reading the latest output. Manual expansion survives history refreshes.
  The summary shows elapsed time from the user message to the final answer
  (or latest recorded step while incomplete). Each nested step shows its persisted
  record time as `HH:MM:SS`, with the full local date/time on hover. These times
  survive reopening; legacy records without timing are not assigned invented times.
- The header's icon-only **观测** control (with a tooltip and accessible name)
  toggles between chat and the Run Trace browser, highlighting while Trace is
  visible. Trace has no separate back button; clicking a recent Session also
  returns to chat and clears the observation highlight. Leaving Trace for another
  page keeps the control state synchronized. Trace controls use the desktop's
  neutral backgrounds, rounded corners, and muted blue-gray selection palette.
  Its adjustable execution
  tree groups each local Run, turn, observed model response, tool execution,
  approval wait, and retry from the SDK's actual Run events. The searchable tree
  shows duration and color-coded status, with one icon-only expand/collapse toggle and
  previous/next navigation through visible nodes. The toggle shows a downward
  triangle while expanded; collapse switches it to a left-pointing triangle with
  the same active background as pin/sidebar. Clicking again expands the tree.
  The detail header shows the
  selected node's status, duration, timestamp, and recorded token/cache counts in
  separate labeled fields. Session is not repeated inside the current-session
  trace browser; detailed usage and IDs remain available in the overview/JSON.
  The default preview displays input and output together; structured payloads
  support expandable fields, JSON text, and copying the sanitized payload.
  Separate tabs retain the overview, observed events, and sanitized raw JSON.
  Live refreshes preserve the selected node, tree expansion, and detail tab.
  Trace data is stored locally in `.sec/desktop-traces.sqlite3` and remains
  available after restarting the desktop app; it is not uploaded. Sensitive
  field names and common credential formats are redacted, and each stored JSON
  payload is bounded. New model attempts record the actual standardized request
  messages and tool definitions, plus the normalized AssistantMessage response.
  This includes prompt/chat text; do not treat redaction as a guarantee that all
  private prose is removed. Thinking content, signatures, and binary attachments
  are omitted. The input is the SEC Provider contract, not the vendor HTTP body.
  Each attempt has a shared logical request ID, its own attempt number, purpose
  (response or context compaction), and running/completed/failed/cancelled status.
  Request duration is measured at the actual Provider boundary with a monotonic
  clock and excludes retry backoff. Separate input/output bounds preserve request
  identity and the response when large inputs require truncation. Truncation
  retains JSON objects/arrays instead of embedding an escaped partial JSON string;
  `_trace_truncated`, `omitted_items`, and `[TRUNCATED]` mark omitted content.
  Adjacent plain text fragments in model payloads are merged before trace limits
  are applied, including fragmented messages from earlier Session history.
  The OpenAI adapter also assembles streamed text into one final TextContent
  block at the source; live text delta events are unchanged.
  Existing traces remain readable; their previously unrecorded fields cannot be
  reconstructed and continue to show “未记录”.
  Token and cost values appear only when present in actual Provider usage;
  approval wait is measured separately from tool execution using a monotonic
  clock, while displayed timestamps use local wall time. Narrow windows stack
  the tree and detail panes vertically.
- Skill/prompt invocations display their command and short user request, not the
  expanded Markdown instructions passed to the model. Display metadata is stored
  separately in the Session, preserving the original model input and export.
  Legacy expanded inputs are shortened when they exactly match a discovered
  resource's content and the CLI resource wrapper; unmatched historical inputs
  are left unchanged rather than guessing or discarding user text.

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

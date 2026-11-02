# 0012 — Runner and embedded terminal (agents inside relaybay)

- Status: **Accepted** (direction), planned for 0.6+ (stage 3). Starts with the "verify before
  start" checks below.
- Size: to be estimated after the checks; roughly runner ~3 agent-days, embedded terminal ~3,
  first ACP adapter ~2.

## Goal

relaybay can start and steer agents itself — always the user's own, unmodified `claude` CLI under the
user's own login — and sessions it starts receive comments and context directly. Interactive
sessions in the desktop app and CLI keep working; this adds, it does not replace.

## Verify before start

1. Does Claude in Chrome (the browser extension) work with a terminal `claude` session started by
   relaybay, the way it works in the desktop app?
2. Does a terminal `claude` session see the user's claude.ai connectors?

The answers decide whether the embedded terminal can replace a desktop session for browser and
connector work, or only for code work.

## Model

- `run`: id, task_id, cwd, prompt, Claude session id, pid, status, cost and duration, started_at,
  ended_at.
- `run_event`: raw stream-json lines, compressed.
- One adapter interface per backend (`start`, `send`, `stop`, `events`) in an `agents/` module:
  `claude -p` stream-json, embedded terminal, Agent Client Protocol.

## Runner (`claude -p`)

- Start: `asyncio.create_subprocess_exec(shutil.which("claude"), "-p", prompt, "--output-format",
  "stream-json", "--verbose", …)` in the repository folder, so the repository's own `CLAUDE.md`,
  skills, settings and `.mcp.json` load exactly as for a manual session. On Windows resolve
  `claude.cmd` / `.exe` via `shutil.which`, no `shell=True`.
- Parse JSON lines (`system/init` → session id; `assistant` / `user` → messages and tool use, subagent
  messages carry `parent_tool_use_id`; `stream_event` partials; `result` → cost, duration, outcome;
  `api_retry`, `permission_denied`, hook progress) into board events. Follow-ups with `--resume
  <session id>`; cancel terminates the process tree.
- Permissions: each project profile sets `--permission-mode` and `--allowedTools` explicitly; never
  skip permissions by default. Never pass `--bare`: it does not use the subscription login.
- The run's MCP config includes relaybay, so the agent reports through the same tools.
- Concurrency limit per machine and per repository; one run per git worktree.

## Embedded terminal

A terminal in the board (pseudo-terminal on the server, a vendored terminal widget in the page,
streamed over a WebSocket or SSE + POST) running interactive `claude` in the project folder. Same
security as the board (cookie, CSRF, Origin), loopback only by default.

## Later in the same direction

ACP adapters for other agents, role profiles (coordinator, manager, developer, reviewer), an
embedded manager per project, the Coordinator and Managers pages and Schedules in the sidebar
([0004](0004-navigation.md)), scheduled workers (0.7+).

## Risks

| Risk | Mitigation |
|---|---|
| Subscription terms for tool-started `claude -p` | Only the user's own binary, never touching tokens; re-check the current terms before building |
| A runaway agent | Explicit permission profiles, concurrency limits, cancel kills the process tree |
| A terminal in the browser widens the attack surface | Same cookie/CSRF/Origin checks, loopback by default, off unless enabled |
| Browser and connector features missing in terminal sessions | The "verify before start" checks decide the scope |

# 0012 — Runner and embedded terminal (agents inside relaybay)

- Status: **Accepted** (direction), planned for 0.7+ (stage 3). Starts with the "verify before
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

The answers come from the Claude Code documentation, marked **docs**, and from one run of
`scripts/probe_claude.py` with a real subscription login on Windows (Claude Code 2.1.312,
2026-11-06), marked **probe**. The script prints the `mcp_servers` names and status of
`system/init`, without and with `--chrome`, and is never run in CI. The probe shows what a session
starts with; no browser action was driven through it.

1. **docs** ([Chrome](https://code.claude.com/docs/en/chrome)): a terminal session gets the browser
   with `claude --chrome`, or without the flag once `/chrome` set "Enabled by default". It needs the
   extension, a direct Anthropic plan and a `/login` sign-in; an API key or a `setup-token` keeps it
   off, and it is not supported in WSL. The page describes interactive sessions and says nothing
   about `-p`. **probe:** `claude -p --chrome` reports a `claude-in-chrome` server as `connected`;
   without the flag it is absent.
2. **docs** ([MCP](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claude-ai)): connectors
   are available in Claude Code automatically, but fetched only while a claude.ai subscription login
   is the active authentication; an API key, an auth token or a `setup-token` hides them.
   `disableClaudeAiConnectors` or `ENABLE_CLAUDEAI_MCP_SERVERS=false` turn them off. **probe:** in
   `-p` the claude.ai connectors are listed in `system/init`; some are `connected`, others `pending`
   at that moment (with `--chrome` nearly all were `pending`) or `needs-auth`. A runner must not
   assume a connector is ready at the first turn.

So the embedded terminal can do browser and connector work, not only code work, under a `/login`
subscription sign-in and with `--chrome` for the browser.

The documented stream shape the runner relies on (`system/init`, `assistant` and `user` with
`parent_tool_use_id`, `system/permission_denied`, a `result` last; SIGTERM runs `SessionEnd` and
exits 143) is imitated by `tests/e2e/fake_claude.py`, checked by `tests/e2e/stream_contract.py`, and
compared with the real CLI by an opt-in test: `RELAYBAY_E2E_REAL_CLAUDE=1 uv run pytest
tests/e2e/test_real_claude.py`.

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
| `--bare` will become the default for `-p` in a future release (docs); bare mode skips hooks, MCP discovery and the subscription login, so a run would lose relaybay and its login | Watch the release notes before each runner change; the contract test fails loudly on a changed stream; the fake refuses `--bare`, so nothing here starts to depend on it. Re-read the headless page before building: how to keep hooks once it is the default is not stated there today |
| `-p` starts in Manual mode from v2.1.302 (docs): a call that needs approval is denied, not asked | Every project profile passes `--permission-mode` and `--allowedTools`; the fake denies in the same way, so a scenario fails without them |

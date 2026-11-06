# 0001 — Hooks and machine setup

- Status: **Implemented in v0.1.0** (step 1.6). Frozen; the behaviour is in
  [SPEC §4](../SPEC.md#spec-hooks) and [SPEC §5](../SPEC.md#spec-setup).
- Size: ~4 agent-days (1.6a receiver ~1.5, 1.6b setup ~1.5, 1.6c Integrations page ~1).

## Goal

relaybay knows which session is working, waiting or gone without the agent's help, and sets up its
own integration on the machine: safely, only on an explicit command or button.

## Model

- `session.cc_session_id` (unique) links Claude Code's own session id to a relaybay session;
  `activity_at` and `unlinked_reason` (migration `0007`). Rows the hooks create have `kind = 'hook'`.
- Hook events map to a status: `SessionStart`, `UserPromptSubmit`, `PreToolUse` → working (tool name
  as activity); `Notification` → waiting; `Stop` → idle; `SessionEnd` → ended and unbound from its
  tasks. Activity is coalesced to one event per session per 5 s.

## API

- `POST /hooks/claude-code` (Bearer token, same Host/Origin rules as `/mcp`), body = hook input JSON,
  capped at 256 KB. `SessionStart` answers with `additionalContext` naming the hook session id.
- `hook_session` on every MCP write tool and `task_bind_session` for exact linking.
- CLI: `relaybay setup [--dry-run | --apply | --remove | --check [--json]] [--expect <hash>]
  [--mode command|http] [--claude-home]`, `relaybay hook`, `python -m relaybay.hook_client`.

## UI

- A status dot on the card's "open in desktop" icon; status and last activity in the task dialog.
- Integrations page: detection, plan with diff and hash, Apply / Remove / Run check, unlinked hook
  sessions.

## Decisions taken while building

- **Exact linking first.** The `SessionStart` answer tells the agent its hook session id; the agent
  passes it as `hook_session`. Linking by `cwd` + `repo_path` is only the fallback, and with more
  than one candidate nothing is linked (listed under Integrations).
- **Command hook by default.** It is silent when relaybay is down and needs no environment variable.
  It runs the interpreter that runs relaybay (`<python> -I -m relaybay.hook_client`), so nothing has
  to be on `PATH`. `--mode http` stays available; its header references the token through
  `${RELAYBAY_TOKEN}` + `allowedEnvVars` and is never written literally.
- **Marker without extra properties.** A command hook is relaybay's when it runs
  `-m relaybay.hook_client` (or `relaybay hook`) with `--url …/hooks/claude-code`; an http hook when
  its URL path is exactly `/hooks/claude-code` on a loopback or configured host. Similar user hooks
  are never claimed.
- **Approved plan.** The dry run prints a plan hash (file before and after); `--expect <hash>` and the
  board's buttons write only that plan.
- **MCP registration is not written.** `~/.claude.json` is Claude Code's own file; setup reads it for
  the check and prints `claude mcp add …`.
- **SessionEnd** unbinds every session kind, for every end reason.
- The minimum Claude Code version for http hooks is not stated in the hooks reference; 2.1.63 is
  used (unverified).
- **Retry and spool for state-bearing events.** A todo change was lost on a slow moment, so
  the forwarder now tells the guard and activity pings (one 0.3 s try, as before) from events that
  carry state (start and end, prompt, notification, Stop, a todo tool's change): those get one
  retry within 2 s and, without an answer, go to a bounded `hook-spool.jsonl` in the config
  folder that the next run sends first. The server takes a replay at its own time and skips one
  older than the session's last change, so an old `SessionEnd` never ends a new start; a replay
  gets no answer and takes no comments. http hooks are not retried or spooled.

## Risks

| Risk | Mitigation |
|---|---|
| Hooks slow down or break sessions when relaybay is down | 0.3 s for the guard and tool calls, at most 2 s for a state-bearing event (only the first while the server is down), silent failure, exit 0; test against a stopped and a hung server |
| Setup damages the user's settings | Backup first, marked merge, refuses invalid JSON, plan hash, idempotent; tests only on temporary homes |
| Wrong card shows a session's status | Exact `hook_session` linking; no guess when ambiguous |
| Token leaks into settings files | Command hook reads the token file; http hooks reference an environment variable |

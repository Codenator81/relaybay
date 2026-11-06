# 0018 — Brake and token budgets

- Status: **Implemented** (unreleased), 0.8.0. Closes the "No way to stop everything or to cap
  spending" row of [0017](0017-readiness-for-agents-inside.md).
- Size: ~3 agent-days.

## Problem

An autonomous agent needs a brake the owner trusts before relaybay starts agents itself. 0017 assumed
relaybay can only warn a desktop session. That is out of date: a hook answer can end a session's turn.

## What actually stops

Claude Code documents that `continue: false` in a hook answer ends processing and wins over event
decisions, that `permissionDecision: deny` on `PreToolUse` refuses the call, and that `SessionStart`
cannot block. `setup` needs no new run: `PreToolUse` is installed and the forwarder prints any JSON
object a 200 carries.

| Measure | How real | Limit |
|---|---|---|
| Stop all, a project, a session, a task | Real at the next tool call | Fails open; see below |
| Token budget, `warn` (the default) | A context line, an Inbox line, an event | Stops nothing |
| Token budget, `stop` (opt-in per budget) | The same brake, set by the budget check | Also lags the reader |
| Concurrent runs | A warning only | Stops nothing |
| With the runner ([0012](0012-runner-and-embedded-terminal.md)) | Kills the process tree | Not built yet |

The brake does not interrupt streaming, a long Bash command or a background process: a process keeps
running after the session stops. It fails open: a server that is down, a read that fails and the
forwarder's 0.3 s try on `PreToolUse` all let the call through. It is not access control: all clients
share one token and an agent's own relaybay tools stay open, so that it can report where it stopped.
Docs answers, not yet seen live: `deny` under a permissive permission mode and `continue: false`
inside subagents. A hook body over 256 KB (a big file write) is dropped unread by the receiver, so
that one call passes under the brake.

## Rules

- **Answer.** `PreToolUse` of any tool except `mcp__relaybay__*` and `ToolSearch` (no side effect,
  and a client that defers MCP schemas needs it to load relaybay's tools; checked on the server only
  until a live run):
  `{"continue": false, "stopReason": r, "hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": r}}`. The check runs before the guard, in
  a separate call, reads only (one query on a partial index, empty almost always) and delivers no
  owner comment: the comment waits for a session that can act on it. The receiver asks it before it
  writes the event and writes after the answer, so a slow or failing write does not lose the stop.
- **Who is covered.** A session by any link, a guessed one too: scope `all`; `project` through the
  session's own project, its bound tasks and the folder it works in, all of them (wider than the
  guard's rule, which asks the folder only without links); `session` that row; `task` while the session is bound to it. A `claude -p` started from a session has its own row:
  `session` does not reach it, `all` and `project` do.
- **Other hooks.** `Stop` does not block and the wake-up long poll ends while braked. The owner's
  prompts are never blocked: `UserPromptSubmit` adds a context line instead.
- **Rows.** An open brake and open task bindings move with the row when a guessed session is merged.
  A braked row, guessed or exact, does not move to another hook id on a write when that id is unknown
  to the hooks or the row is exactly linked and not ended (the "no live now" rule): the real hooks
  would get a row without the brake. A real id the hooks saw carries the brake, and so does the
  desktop window's own `/clear`. A session brake on a row without a hook link reaches nobody yet: the
  answer says so, and so does a task brake on a task with no bound session or one bound to such a
  row. Two projects on one folder: a brake on either holds a session working there.
- **Bindings.** Over MCP, `task_bind_session` and `task_unbind_session` are refused for anyone but a
  coordinator while a brake holds the task or its project (a stop budget's too): they would lift it.
  The board's own bind and unbind stay with the owner. Scope `all` is left out: a binding cannot lift
  it.
- **Who.** The owner on the board and coordinators over MCP set it; only the owner on the board
  releases. A second brake on the same target answers with the open one; a budget's own brake is a row
  apart, so releasing one never lifts the other.
- **Budgets.** One per target: all projects, a project, a task; per UTC day or in total. Counted: the
  Usage page's token total, cache reads included. A task counts lines written while a session was
  bound to it, read from the line's own time; a session on several tasks counts in full on each. Usage
  read before migration 0026 has no task and is not backfilled. Tokens read while the binding sits on
  a row without a hook link are on the hook's row and do not count for the task, then or after the
  rows merge: the project and all-projects budgets still count them. A project or all-projects `total`
  counts what the Usage page has, older usage too.
- **Thresholds.** 80% and 100%, checked after each usage pass, never on the hook path, at least once a
  minute while a budget or a cap exists. Each counts once per period: the mark, the event and the brake
  are one transaction, so a failure repeats the whole check and a released brake does not come back in
  its period. Changing a budget forgets its marks; the same numbers again change nothing.
- **Release.** A daily budget's brake is released by the first check of a new UTC day
  (`released_by = "new day"`); all others only by the owner. Removing a budget leaves its brake.
- **Reading off.** With `usage_enabled = false` nothing new is counted: the page says budgets are
  blind and `doctor` shows it. The check still runs for the new day and the cap.
- **Concurrency.** `max_concurrent_sessions`, default 0 (off): sessions `working` with activity in the
  last 10 minutes above the cap give one `concurrency.over` event per 10 minutes and an Inbox line.

## Decided by default

The owner's defaults, each reversible: `warn` with `stop` opt-in per budget; the Usage page's total;
the UTC day; full amount on each task of a session; owner and coordinators set, only the owner
releases; cap 0. No release and no budget write over MCP.

- One budget per target, as the plan says, not one per target and period.
- A released brake stays released for that period, however far usage goes.
- The board stops all agents or a project's; session and task scope are for coordinators over MCP.
- `task_bind_session` and `task_unbind_session` gain `caller` (and the latter `hook_session`), since
  their `session` names the target; without `caller`, the target counts as the caller.

## Risks

- The brake stops a session in the middle of a deployment: hence `warn` by default.
- A budget lags the reader by up to `min(usage_interval_seconds, 60)` seconds plus the reading itself;
  the agent can spend well past 100% before it stops.
- `/clear` of a named session under a session brake gives its new hook id a fresh row, outside the
  brake. Desktop sessions keep their row.

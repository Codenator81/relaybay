# 0012 — Runner and embedded terminal (agents inside relaybay)

- Status: **Accepted** (direction); the runner is planned for 0.9, the terminal for 0.10
  ([0021](0021-agents-inside.md)). Starts with the probes below.
- Size: runner about 4 agent-days (0.9, PR 1 of [0021](0021-agents-inside.md#plan-of-09-by-pull-request)),
  terminal about 4 (0.10), first ACP adapter about 2 (not before the runner has run a week).
- 2026-11-10: the runner is a headless, step-by-step runner with `--resume`, and the terminal opens
  the same session. The overall design is [0021](0021-agents-inside.md); questions and approvals are
  [0022](0022-questions-and-approvals.md).

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

## Probes 2026-11

Six more answers before any runner code, marked **docs** and **probe** like the two above. The
probe is one run of `scripts/probe_runner.py` (opt-in, never in CI: it refuses when `CI` is set)
with a real subscription login on Windows (Claude Code 2.1.312, 2026-11-10, model haiku, a temporary
relaybay server and a temporary git repository with a bare remote; the user's own hooks kept out
with `--setting-sources project` and `--settings <file>`). It spent about 0.05 USD of estimated cost.

| # | Probe | docs | probe | For the runner |
|---|---|---|---|---|
| 1 | Resume | | **pass.** A second turn with `--resume <id>` kept the session id and remembered the first turn; relaybay's MCP server `connected` on both turns; the repository's `CLAUDE.md` read; skills and commands listed. Hooks fired on every turn (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd` per turn, plus tool events) | The step-by-step headless runner works as designed |
| 2 | Permissions in `-p` | Manual is the default for `-p` from v2.1.302. Approval inside a turn is documented through a `PermissionRequest` hook or `--permission-prompt-tool <mcp tool>` | Modes offered: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. In the default mode a Bash `touch` was **denied** without a prompt, listed in `result.permission_denials`, the model told; the turn ended normally (`success`), no file made. The prompt tool itself not probed | A denial or the agent's own `approval_request` becomes an approval on the board, and the owner's decision a new turn ([0022](0022-questions-and-approvals.md)). The prompt tool or hook is a documented later upgrade |
| 3 | The owner's word | | **pass** in `--permission-mode auto`: turn 1 "the owner has not approved, do not push" gave "waiting" and no tool; resumed turn 2 "approval from the owner, given on the board: push is approved" made the agent run `git push origin main`, and the remote moved to the local HEAD. Not probed: a protected remote, a forged approval inside a tool result | A new turn carries the owner's word; no `allow` answer from the program is needed for the first slice |
| 4 | Concurrent access | | Two headless turns on one session id at once (the second 8 s into the first): both exit 0, no lock, no fork, same session id, the transcript interleaved. Interactive `claude --resume` during a headless turn not probed (needs a PTY) | The runner serializes turns per run itself; while a terminal is open (0.10), headless turns for that run queue |
| 5 | Bare and subscription terms | [Headless](https://code.claude.com/docs/en/headless): `--bare` skips hooks, `CLAUDE.md`, MCP discovery and the subscription login (API key only) and "will become the default for `-p`" in a future release. [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance): subscription sign-in is meant for ordinary use of Claude Code and Anthropic's own applications; products built on the Agent SDK should use an API key; third parties may not route requests through plan credentials for their users or collect credentials; the same page says nothing stops a user from signing in to the unmodified binary with their own subscription. The `-p` page presents it as running Claude Code programmatically | | relaybay never passes `--bare`, never touches credentials, runs the unmodified binary on the user's own machine under the user's own login. That reads as inside the user's own use, but programmatic turns are a grey area: a question for the owner before code ([0021](0021-agents-inside.md#questions-for-the-owner)) |
| 6 | Token numbers | | `result.usage` per turn (input, output, cache read, cache write), summed over four turns of one session (two in sequence, two at once), equals the transcript's sums exactly. `total_cost_usd` is a client estimate and on resume reports the conversation's total | A turn's `result.usage` can show on the card live; it agrees with what the budgets of [0018](0018-brake-and-budgets.md) count from the transcript |

## Model

- `run`: one per card, as [0021](0021-agents-inside.md#the-run) describes: card, cwd, worktree, model,
  effort, permission profile, Claude session id, status, who started it, started_at, ended_at.
- `run_turn`: one per turn: prompt, who sent it, pid, started and ended, outcome, tokens from `result`.
- `run_event`: the stream-json lines of each turn, compressed, secret shapes masked, kept by the
  project's retention policy.
- One adapter interface per backend (`start`, `send`, `stop`, `events`) in an `agents/` module:
  headless turns with `--resume`, the embedded terminal, later the Agent Client Protocol.

## Runner (step by step, headless)

- First turn: `asyncio.create_subprocess_exec(shutil.which("claude"), "-p", prompt, "--output-format",
  "stream-json", "--verbose", "--permission-mode", …, "--allowedTools", …)` in the run's worktree, so
  the repository's own `CLAUDE.md`, skills, settings and `.mcp.json` load exactly as for a manual
  session. On Windows resolve `claude.cmd` / `.exe` via `shutil.which`, no `shell=True`. The
  environment carries `RELAYBAY_RUN_ID`, which the hook forwarder sends with each event.
- Every later turn: the same with `--resume <session id>` and the turn's text (the owner's answer, an
  approval, an owner comment, a coordinator's message framed as data). One turn at a time per run;
  texts arriving during a turn queue.
- Parse JSON lines (`system/init` → session id; `assistant` / `user` → messages and tool use, subagent
  messages carry `parent_tool_use_id`; `stream_event` partials; `result` → tokens, duration, outcome;
  `api_retry`, `permission_denied`, hook progress) into board events. Stop terminates the turn's
  process tree; the session stays resumable.
- Permissions: each project profile sets `--permission-mode` and `--allowedTools` explicitly; never
  skip permissions by default. Never pass `--bare`: it does not use the subscription login.
- The run's MCP config includes relaybay, so the agent reports through the same tools.
- Slots per machine and a free-memory check before each turn; one run per worktree.
- A one-shot `-p` without `--resume` only for schedules and read-only checks
  ([0023](0023-coordinator-and-schedules.md)).

## Embedded terminal

0.10. A Terminal tab on a run (pseudo-terminal on the server, a vendored xterm.js in the page,
streamed over a WebSocket) running `claude --resume <session id>` in the run's worktree: the same
session the headless turns use. Off until the owner turns it on; same security as the board (cookie,
CSRF, Origin), loopback only. While it is open, headless turns for that run queue (exact rule from
probe 4). Platform notes: [0021](0021-agents-inside.md#platform-notes).

## Later in the same direction

The coordinator as a permanent run and schedules (0.11, [0023](0023-coordinator-and-schedules.md)),
role profiles editable on the board (0.12); a manager per project only if a week of coordinator work
shows the need; ACP adapters for other agents and containers not before the runner has run a week.

## Risks

| Risk | Mitigation |
|---|---|
| Subscription terms for tool-started `claude -p` | Only the user's own binary, never touching tokens; re-check the current terms before building |
| A runaway agent | Explicit permission profiles, concurrency limits, cancel kills the process tree |
| A terminal in the browser widens the attack surface | Same cookie/CSRF/Origin checks, loopback by default, off unless enabled |
| Browser and connector features missing in terminal sessions | The "verify before start" checks decide the scope |
| `--bare` will become the default for `-p` in a future release (docs, probe 5); bare mode skips hooks, `CLAUDE.md`, MCP discovery and the subscription login, so a run would lose relaybay and its login | Never pass `--bare`; the opt-in real-`claude` test checks on every run that a `-p` turn still fires hooks and connects relaybay's MCP server, so a flipped default fails loudly; watch the release notes before each runner change; the fake refuses `--bare` |
| Two turns on one session at once interleave the transcript (probe 4) | One turn at a time per run, enforced by the runner; texts arriving during a turn queue |
| `-p` starts in Manual mode from v2.1.302 (docs): a call that needs approval is denied, not asked | Every project profile passes `--permission-mode` and `--allowedTools`; the fake denies in the same way, so a scenario fails without them |

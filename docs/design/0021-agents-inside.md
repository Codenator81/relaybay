# 0021 — Agents inside the program

- Status: **Proposed**, 2026-11-10. The overall design of agents started and steered by relaybay,
  releases 0.9 to 0.12. Details live in [0012](0012-runner-and-embedded-terminal.md) (runner,
  terminal, probes), [0022](0022-questions-and-approvals.md) (questions and approvals as objects)
  and [0023](0023-coordinator-and-schedules.md) (coordinator and schedules). Closes the direction of
  [0017](0017-readiness-for-agents-inside.md); uses [0018](0018-brake-and-budgets.md),
  [0019](0019-session-messages.md), [0013](0013-policy-as-data.md), the pages planned in
  [0004](0004-navigation.md) and the autonomy rule of [0015](0015-intake-and-autonomy.md).
- The probes of 0012 were run on 2026-11-10 and the design relies on them
  ([0012](0012-runner-and-embedded-terminal.md#probes-2026-11)). No code before the coordinator
  accepts this design and the owner answers [O1](#questions-for-the-owner).
- Size: 0.9 about 12 agent-days ([plan by PR](#plan-of-09-by-pull-request)); 0.10 about 4; 0.11
  about 6; 0.12 about 2.

## Goal

The owner drives a task from start to end in relaybay without opening the desktop chat: card, agent,
question, answer on the board, acceptance, approval of the push. Each release is measured by one real
task driven that way.

## The pain it removes

Today the owner talks to a long-lived coordinator session; the coordinator starts one desktop session
per task, writes the brief by hand, relays the owner's answers and approvals, and reads reports back.
relaybay watches it all through hooks and MCP.

| # | Pain | What removes it | Release |
|---|---|---|---|
| 1 | The coordinator sees only the first 400 characters of a session's report and has to read the transcript or ask again | The report of every turn lives on the card in full | 0.9 |
| 2 | Claims nobody checked are retold as facts ("the migrations will apply" while the test stand was down for two hours) | A `verified: <where>` mark on claims; a rules warning on an M or L report without marks | 0.9 |
| 3 | A question and its answer go through retelling; which session asked what, and which is still open, gets lost ("I got confused about which session is asking what") | A question is an object on its card, with options, a default and the owner's answer ([0022](0022-questions-and-approvals.md)) | 0.9 |
| 4 | A second task in the same session goes worse than in a fresh one | One run per card, a fresh Claude session per run | 0.9 |
| 5 | Two sessions with heavy builds at once froze a 16 GB machine and took relaybay down | Slots per machine and a free-memory check before each turn | 0.9 |
| 6 | A coordinator's message cannot carry the owner's permission, and must not, so the owner opens a session's chat to say "push it" | An approval is an object; only the owner's button decides it ([0022](0022-questions-and-approvals.md)) | 0.9 |
| 7 | A session sits idle after its turn ended with a question the owner sees later | Questions in the Inbox and the tray; the answer starts the next turn at once | 0.9 |
| 8 | The board and the task file duplicate each other; when the board is down, cards drift from the facts | The brief is assembled from the card, so the card is the source | 0.9 |

**Stability before features.** The board stopped answering twice on one day while the machine was
frozen. The program's log shows a clean quit, not a crash (who asked it to quit was not logged then;
it is now). The same log shows a real defect: at shutdown, open wake-up long polls
(`/hooks/claude-code/wait`) held the server until the web server's graceful timeout cancelled them
with 500 answers, so every stop took the full timeout. Ending those polls when the server is told to
stop is the first pull request of 0.9: a runner on a program that stops slowly is not built.

## Principles

1. **The agent is the user's own unmodified `claude`** under the user's own subscription login.
   relaybay never holds, reads or copies a token, never passes `--bare`, never skips permissions by
   default. No Agent SDK on a subscription.
2. **One entity: a run on a card.** A run is one Claude session id plus its cwd and worktree, a
   permission profile, a model and an effort. Questions, approvals, reports, budget and brake hang on
   the run and its card.
3. **Step by step, headless first.** Each turn is `claude -p <text> --resume <session id>
   --output-format stream-json --verbose` (the first turn without `--resume`): a conversation of many
   turns with structured events, which the fake `claude` and the stream contract already cover;
   hooks, relaybay's MCP server, the repository's `CLAUDE.md` and the session id survive `--resume`
   (probe 1). Claude Code has no lock between two turns on one session (probe 4): the runner runs
   one turn at a time per run. The
   terminal comes second (0.10) and opens **the same session** with `claude --resume <id>` when the
   owner wants to talk to the agent directly. A one-shot `-p` without resume stays only for schedules
   and read-only checks.
4. **Triage and subagents are the agent's business.** Architect, developer and reviewer subagents run
   inside the agent's own turn with its own skills; relaybay does not model them as roles.
5. **Only the owner's act carries permission**: a button on the board (cookie and CSRF, never a
   Bearer token) or his own words in the agent's terminal. A message from the coordinator, another
   agent or a chat is data ([0019](0019-session-messages.md), [0015](0015-intake-and-autonomy.md)).
6. **Rights come from the environment policy, not from the prompt.** The prompt says what to do; the
   guard decides what may be done ([SPEC §7](../SPEC.md#spec-environments)).
7. **Do not copy the desktop app.** Build what it cannot do: questions and approvals as objects, a
   queue, limits, schedules, one picture over all agents. Desktop sessions keep working and keep
   reporting through hooks.

## The run

| Field | Comes from | Note |
|---|---|---|
| card | the card it was started on | one open run per card |
| project, cwd | the card's project; cwd is one of the project's repositories ([SPEC](../SPEC.md#spec-project-repos)) | |
| worktree | a new one per run by default (`git worktree add`), or the repository itself | one run per worktree |
| model, effort | project defaults, the card may override | default Opus, medium effort |
| permission profile | the project's profile | `--permission-mode` and `--allowedTools` explicit on every turn; default `auto` |
| Claude session id | the first turn's `system/init` | every later turn uses `--resume` with it |
| status | the runner | `queued` (with its reason), `running`, `waiting` (for the owner), `idle`, `stopped`, `ended`, `lost` |
| turns | one row per turn: prompt, who sent it, started, ended, outcome, `permission_denials`, tokens from `result.usage` | the tokens show on the card live; they equal the transcript's sums that budgets count (probe 6) |
| events | `run_event`: the stream-json lines of each turn, compressed, kept by the retention policy | subagent lines nested by `parent_tool_use_id` |

**The brief.** The first turn's text is assembled from the card: title, description, decisions with
their source, the accepted plan, attachments as file paths, the project's rules line, the report
format. It is shown before Start and can be edited for that run only.

**Turns.** Start (the first turn), continue (`--resume` with the owner's answer, an approval, an owner
comment, or a coordinator's message framed as data; texts that arrive during a turn queue for the
next one), stop (kill the current turn's process tree; the
session stays resumable), end (no more turns). Between turns no `claude` process runs, so an idle run
holds no memory and no slot.

**Limits.** Slots per machine (default two heavy turns at once) and a free-memory check before each
turn. Too little memory or no free slot: the turn is **queued with its reason** on the card and in the
Inbox ("waiting for a slot: 2 of 2 running", "waiting for memory: 1.8 GB free, 3 GB needed"), never
refused silently; the owner may "start anyway". The brake and the budgets of 0018 act on runs: a
braked run takes no new turn, and the current one is killed after its next refused tool call.

**Lifecycle.** A card moved to done ends its run. `run_event` is kept by the project's retention
policy; a run marked "saw a secret" or one that touched a live environment has its events and its
Claude Code transcript deleted when the card closes.

**Reports.** Each turn's final report lands on the card in full; subagent work shows as nested events
under it. A claim may carry `verified: <where>`; an M or L report without any marks gets a rules
warning.

## Who may start a run

| Who | May | How it holds |
|---|---|---|
| The owner | always, any card | the board button |
| The coordinator | cards in `plan` or `dev`, within slots and budgets, no brake on the target | MCP `run_start`, caller in the `coordinators` config |
| Any other agent | never a top-level run; subagents inside its own turn are its own | `run_start` refuses; a side task goes to the backlog |

The caller check is the same weak identity as `rules_update` (one shared token): it stops mistakes,
not a determined agent.

## Safety model

- **Rights.** The guard and access map decide each call, for runs as for desktop sessions. The
  approval rules of [0022](0022-questions-and-approvals.md) sit on top: what needs an approval is
  policy data (0013); by default a push and anything that touches a stand; a project may allow a push
  without approval; live always needs one and this is not configurable. **No live now** works on run
  sessions unchanged.
- **Secrets.** Not in relaybay's database, with one exception that has to be decided: `run_event`
  holds what the agent's tools printed, and a tool can print a secret. Mitigation: the journal's
  secret masking is applied to every stored line, the "saw a secret" mark deletes on close, and the
  retention policy can turn storage off ([question C3](#questions-for-the-coordinator)).
- **On the owner's machine the program may**: start `claude` from `PATH` in a project's repository or
  a worktree of it; run `git worktree add` and, on the owner's button, `git worktree remove`; kill the
  process trees it started; delete a transcript file of its own run under the Claude Code data
  folder; read free memory. **It may not**: run git commit, push or any other git write itself;
  change the user's Claude Code settings outside `relaybay setup`; read credentials; pass
  `--dangerously-skip-permissions` or `--bare`.
- **Stopped for sure.** Stop or the brake: no new turn, the current tool call refused (0018), then
  the process tree killed: a Windows job object created at start (`TerminateJobObject`), a process
  group on macOS and Linux (`killpg`, SIGTERM, then SIGKILL after 10 seconds). Builds and servers the
  agent started die with it. A run whose process vanished without a `result` is marked `lost`.
- **Audit page.** Runs and turns (who started, who stopped, why), questions asked and answered,
  approvals requested, decided and used, schedule runs, deletes of events and transcripts, queueing
  for slots and memory, besides the actions on live it shows today.

## Platform notes

The headless runner (0.9) needs no PTY: a subprocess with pipes, `shutil.which` for `claude.exe` or
`claude.cmd` on Windows, no shell. The terminal (0.10) needs one:

| | Windows | macOS | Linux |
|---|---|---|---|
| PTY | ConPTY through `pywinpty` (MIT; a new dependency) | stdlib `pty` | stdlib `pty` |
| Process tree | job object | process group | process group |

**Terminal widget.** xterm.js (MIT, about 300 KB with the fit add-on) vendored into the static files
with its licence, no CDN, talking to the local server over a WebSocket; the system web views
(WebView2, WKWebView, WebKitGTK) run it. Fallback, if the `claude` TUI renders badly there: open the
user's own terminal app with `claude --resume <id>`; the board then shows only hook events for that
time. The terminal is off until the owner turns it on, loopback only, cookie, CSRF and Origin as the
board. While it is open, headless turns for that run wait in its queue (probe 4 of 0012 decides the
exact rule).

**Probes.** The six probes of [0012](0012-runner-and-embedded-terminal.md#probes-2026-11) answered
the headless questions on Windows. Still open, for 0.10: interactive `claude --resume` in a PTY while
a headless turn runs, rendering of the TUI in each system web view, and the same probes on macOS and
Linux.

## Questions for the owner

Only these stay with the owner; everything else the coordinator decides.

| # | Question | Default | Why the owner |
|---|---|---|---|
| O1 | The terms allow signing in to the unmodified binary with one's own subscription and forbid third parties routing requests through plan credentials or collecting them; programmatic `-p` turns started by a program are a grey area (probe 5 of 0012). Go ahead? | Go ahead; runs only on the owner's own machine under his own login, relaybay never touches credentials, and the README says so | Terms of the owner's account |
| O2 | Anything that sends data outside the machine (none planned) | Nothing leaves the machine | Data leaving the machine |
| O3 | README promises and the licence (none change) | Unchanged | Public promises |

## Questions for the coordinator

| # | Question | Default | Reason |
|---|---|---|---|
| C1 | First slice | Headless step-by-step runner with questions, approvals, limits and lifecycle in 0.9; terminal in 0.10 | Structured events and the fake already exist; the terminal adds nothing without questions and approvals |
| C2 | Default permission mode for a run | `auto`, with explicit `--allowedTools`; bypass only by the owner's word per card | Probe 3 passed in `auto`. In Manual (the `-p` default) a call needing approval is denied and the turn ends normally (probe 2); a denial becomes an approval request either way |
| C3 | Keep `run_event` in the database | Yes, masked, deleted on close for "saw a secret" or live runs, retention `keep 30 days` per project | Full reports and nested subagent events need it; the masking is the journal's |
| C4 | Slots and memory | 2 heavy turns at once; 3000 MB free before a turn | Two heavy builds froze a 16 GB machine |
| C5 | `psutil` for free memory | No; platform code (`GlobalMemoryStatusEx`, `sysctl`, `/proc/meminfo`) | One dependency less in the frozen build |
| C6 | Default model and effort | Opus, medium; the other model as a card field set by the coordinator or the owner | Today's practice |
| C7 | One coordinator for all projects | Yes ([0023](0023-coordinator-and-schedules.md)) | Links and priorities across projects are visible only from above |
| C8 | Managers layer | Not built; decided after a week of coordinator work | A layer of retelling is the problem being removed |
| C9 | ACP adapters and containers | Not before the runner has run a week (0017) | Same as 0017 |
| C10 | Approval delivery: a new turn, or approval inside a turn | A new turn with the owner's words (probe 3: the agent pushed). `--permission-prompt-tool` or a `PermissionRequest` hook is a documented later upgrade, not probed | A turn is the owner's word to the agent; approving inside a turn is a wider mechanism |

## Releases

| Release | Content | Acceptance |
|---|---|---|
| 0.9 | The shutdown fix; the runner on a card (headless, `--resume`); questions and approvals as objects; slots and memory; lifecycle; full reports with `verified` marks | One real task of size S or M from card to push without the desktop chat; the owner answers one question and gives one approval on the board |
| 0.10 | Terminal tab on a run (PTY, xterm.js, `claude --resume <id>`), off by default; headless turns queue while it is open | A step-by-step acceptance by eye done in relaybay's terminal |
| 0.11 | The coordinator as a permanent run, woken by events; schedules; the Coordinator page ([0023](0023-coordinator-and-schedules.md)) | A working day in which the coordinator inside relaybay sets and drives the tasks |
| 0.12 | Role profiles editable on the board; managers only if the week of 0.11 shows the need | A profile changed on the board takes effect on the next turn |
| 1.0 | As in the roadmap, plus a week of the owner's daily work through relaybay without going back to the desktop app for anything relaybay should do | |

Each release goes onto the owner's machine and runs there before the next one starts; findings from
practice go ahead of the plan.

## Plan of 0.9 by pull request

| PR | Content | Migrations | Size |
|---|---|---|---|
| 0 | End the wake-up long polls (`/hooks/claude-code/wait`) when the server is told to stop, so a stop no longer waits for the graceful timeout | none | S |
| 1 | The run: task defaults on the project (model, effort, profile), `run` and `run_turn` tables, the runner (first turn, `--resume`, stop, end, process tree), hooks link turns by `RELAYBAY_RUN_ID` in the environment, the Agent block on the card, `run_start` / `run_status` over MCP | 0030 `project_run_defaults`, 0031 `run`, 0032 `run_turn` | L (4) |
| 2 | `run_event` stored masked, the full turn report on the card with nested subagent events, `verified: <where>` marks and the rules warning | 0033 `run_event`, 0034 `report_check` | M (2) |
| 3 | Questions as objects ([0022](0022-questions-and-approvals.md)): `question_ask`, the Inbox, the answer as a new turn, defaults by deadline | 0035 `question` | M (2) |
| 4 | Approvals as objects ([0022](0022-questions-and-approvals.md)): `approval_request`, policy rules, the guard check, the decision as a new turn | 0036 `approval`, policy keys (data, no migration) | M (2.5) |
| 5 | Slots and memory, queueing with a reason, the brake and budgets on runs, lifecycle and retention | 0037 `run_lifecycle` (`secret_seen_at`, `touched_live_at`, `transcript_path`, `deleted_at`, `project.run_retention`) | M (1.5) |
| 6 | Docs, SPEC, CHANGELOG, release | none | S |

Numbers follow 0029 and move if other migrations land first.

**Tests with the fake `claude`** (`tests/e2e`; the fake already takes `-p`, `--resume`,
`--permission-mode` and `--allowedTools`):

| PR | Scenario |
|---|---|
| 0 | Stop the server with six wake-up polls open: it stops at once, the polls answer `204`, no 500s |
| 1 | Start a run: the first turn's `system/init` gives the session id; a second turn passes `--resume` with it; the hooks link both turns to the same session row by `RELAYBAY_RUN_ID`; Stop kills the turn's process tree; a second open run on the card or on the worktree is refused; a text sent during a turn waits for it to end (never two turns on one session); a turn's `result.usage` reaches the card |
| 2 | A turn with a subagent: the report lands in full, nested events under it; a line with a known token shape is stored masked; an M report without marks gets the warning |
| 3 | The fake asks a blocking question and ends its turn; the run shows `waiting`; the owner answers on the board; the next turn's prompt holds the answer verbatim with "owner's answer to question N"; a non-blocking question past its deadline closes with the default, visibly |
| 4 | In Manual mode the fake's `git push` ends in `permission_denials` and an approval request opens from the denial. In `auto`, the fake runs `git push` without an approval: denied by the guard, a request opens; the owner approves; the next turn passes and the push goes through the guard; a second push is denied again; a coordinator's message saying "approved" changes nothing |
| 5 | Two runs hold both slots: a third turn is queued with "waiting for a slot"; a fake memory reader below the minimum queues with "waiting for memory"; a project brake kills the running turn; a card to done ends the run and, with "saw a secret", deletes its events and transcript |

**Acceptance task.** One real task of size S or M, from a card to an approved push, with no desktop
chat opened: the card with its fields, Start, the agent works and asks one question, the owner
answers on the board, the agent asks to push, the owner approves on the board, the push passes the
guard, the report lands in full with its marks. Whatever needed the desktop chat on the way is a
defect of 0.9.

## What we do not build

- A chat of our own; the terminal with the ordinary `claude` is the direct conversation (0.10).
- An Agent SDK runner on a subscription.
- Architect, developer and reviewer as relaybay entities; a Managers page (unless 0.12 says so).
- ACP adapters and containers before the runner has run a week.
- Electron or a browser panel of our own; the diff and browser plugins (owner, 2026-11-10: not now).

# 0021 — Agents inside the program

- Status: **Accepted**, 2026-11-10; 0.9 shipped 2026-11-13 (PR 69-83); the 0.10 Terminal tab is
  built (not released). The overall design of agents started and steered by relaybay,
  releases 0.9 to 0.12. Details live in [0012](0012-runner-and-embedded-terminal.md) (runner,
  terminal, probes), [0022](0022-questions-and-approvals.md) (questions and approvals as objects)
  and [0023](0023-coordinator-and-schedules.md) (coordinator and schedules). Closes the direction of
  [0017](0017-readiness-for-agents-inside.md); uses [0018](0018-brake-and-budgets.md),
  [0019](0019-session-messages.md), [0013](0013-policy-as-data.md), the pages planned in
  [0004](0004-navigation.md) and the autonomy rule of [0015](0015-intake-and-autonomy.md).
- PR 1a shipped: the run on a card in one repository used in place (migrations 0030
  `project_run_defaults` and `app_setting`, 0031 `run`, 0032 `run_turn`), the login check with the
  doctor line and the Settings → Agents switch, project agent defaults, first turn, `--resume`,
  waiting texts, Stop with the process tree, End, lost, linking by `RELAYBAY_RUN_ID` (the http
  hook's `X-Relaybay-Run` header) and the Agent block; Start refuses while relaybay's hooks or MCP
  server are not in the user settings. PR 1b brings `run_worktree` (0033), worktrees with
  `--add-dir`, the project folder as cwd, the `--settings` / `--mcp-config` fallback and MCP
  `run_start` / `run_status`. Behaviour: [SPEC](../SPEC.md#spec-run).
- PR 1b shipped: migration 0033 `run_worktree`; the project folder (agent defaults) as the cwd; a
  new `git worktree add` per repository by default (or the repository itself), each passed with
  `--add-dir` and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`; the worktrees a failed Start
  made are removed again; the untracked Claude Code files copied into a new worktree (decision Q3
  below); relaybay's hooks and MCP server passed with `--settings` / `--mcp-config` when the user
  settings lack them, replacing 1a's refusal; MCP `run_start` for coordinators and `run_status` for
  everyone; one open run per worktree path. Removing worktrees stays with PR 5.
- PR 2 shipped: migrations 0035 `run_event` (every stream-json line of a turn, each string masked
  with the journal's `redact` before the line is compressed with zlib, `parent_tool_use_id` kept,
  a line over 4 MiB as its masked head; stored in batches of 200 lines or 0.5 s) and 0036
  `report_check`; the turn's final text as a full report on the card with its refusals, the Agent
  block's folded report and nested events per turn, `verified: <where>` marks from runs and
  `report_add` with the rules warning on an M or L report without any, MCP `run_events` and the
  report line and event counts in `run_status`. Retention and deletes stay with PR 5 (C3).
- PR 3 shipped: migration 0037 `question` (0036 went to `report_check`; 0038 then went to the
  manual archive, so approvals took 0039 and the lifecycle takes 0040); questions and "look at this" as objects on the card and in the Inbox,
  MCP `question_ask` / `question_list` / `question_withdraw` (no tool answers), the answer on the
  board as the run's next turn or a desktop session's owner comment, a run `waiting` on a blocking
  question, non-blocking questions closed with their default at the deadline, the tray kind
  `question` ([0022](0022-questions-and-approvals.md#questions); behaviour:
  [SPEC](../SPEC.md#spec-questions)).
- PR 4 shipped: migration 0039 `approval`; approvals as objects with the policy key `approvals`
  (live never droppable; in 0.9 for the program's runs only, desktop sessions with
  `approvals.desktop`), the guard's check of `git push` (with its branch), `gh pr merge` and
  stand changes, one approval per call (reserved for the call at the allow, used once it ran,
  released when Claude Code refused it, expired at the third release:
  [0022](0022-questions-and-approvals.md#reserved-used-released)), requests from a turn's
  `permission_denials`, MCP `approval_request` / `approval_list` (no tool decides), the Inbox and
  the card, the decision as the next turn or an owner comment, the tray kind `approval`
  ([0022](0022-questions-and-approvals.md#approvals); behaviour:
  [SPEC](../SPEC.md#spec-approvals)).
- PR 5 shipped: migration 0040 `run_lifecycle`; `max_running_turns` and `min_free_memory_mb`
  (config.toml, editable in Settings → Agents) checked before each turn with the brake, a turn
  `queued` with its reason on the card and in the Inbox, FIFO over all runs, **Start anyway** past
  the slot limit and the memory check (never past a brake); free memory by platform code (C5); a
  braked run's turn killed after its refused call; a card to done ends its run, removes the
  worktrees it created and asks about one with work left; "saw a secret" and "touched live" runs
  lose their lines and transcript on close; per-project retention (C3) (behaviour:
  [SPEC](../SPEC.md#spec-run-limits), [lifecycle](../SPEC.md#spec-run-lifecycle)).
- The probes of 0012 were run on 2026-11-10 and the design relies on them
  ([0012](0012-runner-and-embedded-terminal.md#probes-2026-11)). No code before the coordinator
  accepts this design; the owner answered [O1](#questions-for-the-owner) (go ahead).
- Size: 0.9 about 13 agent-days ([plan by PR](#plan-of-09-by-pull-request)); 0.10 about 4; 0.11
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
| project, cwd | the card's project; cwd is the project folder (a container of repositories, not itself a git repository) or one of the project's repositories ([SPEC](../SPEC.md#spec-project-repos)) | the cwd's own `CLAUDE.md` loads |
| worktrees | a list, one per repository the task touches, chosen at Start from the project's repository list (migration 0029); each a new `git worktree add` by default, or the repository itself | one run per worktree, for each of them; every worktree is passed with `--add-dir`, and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` is set so each repository's `CLAUDE.md` loads; the cwd alone gives only its own `CLAUDE.md` (probe 9) |
| model, effort | project defaults, the card may override | default Opus, medium effort |
| permission profile | the project's profile | `--permission-mode` and `--allowedTools` explicit on every turn; default `auto` |
| Claude session id | the first turn's `system/init` | every later turn uses `--resume` with it |
| status | the runner | `queued` (with its reason), `running`, `waiting` (for the owner), `idle`, `stopped`, `ended`, `lost` |
| turns | one row per turn: prompt, who sent it, started, ended, outcome, `permission_denials`, tokens from `result.usage` | the tokens show on the card live; they equal the transcript's sums that budgets count (probe 6) |
| events | `run_event`: the stream-json lines of each turn, compressed, kept by the retention policy | subagent lines nested by `parent_tool_use_id` |

**The Claude Code layers.** A run loads what a manual session loads: Claude Code's default
`--setting-sources user,project,local`, which relaybay never narrows. The user `~/.claude/CLAUDE.md`
(the owner's common rules) loads whatever the sources say; user skills and agents (a pipeline skill,
architect, developer and reviewer agents), the output style and user MCP servers come only with the
default sources (probe 8: 92 commands and 9 agents against 58 and 6 with `project` only). Then the
project's and the local layer of the cwd. (The first probes used `--setting-sources project` only to
keep the owner's own hooks from reaching his live board; `--settings '{"disableAllHooks": true}'`
does that without narrowing anything.)

**relaybay's hooks without double firing.** relaybay's hooks are the command hooks `relaybay setup`
installs into the user's settings. Before the first turn the runner reads the same check as `relaybay
setup --check`: if the user's settings carry relaybay's hooks and MCP server (the normal case), the
run adds nothing; if not, the run passes them with `--settings <file>` and `--mcp-config <file>`
(never `--strict-mcp-config`). Claude Code runs an identical handler defined in several settings
sources once (documented, and probe 8: a byte-identical command in two sources fired once), so the
run writes exactly the command `setup` writes; if the check was wrong and both are present, nothing
fires twice. relaybay adds no dedupe of its own on the hook path, which must stay fast; the primary
path stays "add none when setup installed them", because the documentation does not say how two
handlers are judged identical.

**The brief.** The first turn's text is assembled from the card: title, description, decisions with
their source, the accepted plan, attachments as file paths, the project's rules line, the paths of
the run's worktrees and which repository each is, the report format. It is shown before Start and
can be edited for that run only.

**Turns.** Start (the first turn), continue (`--resume` with the owner's answer, an approval, an owner
comment, or a coordinator's message framed as data; texts that arrive during a turn queue for the
next one), stop (kill the current turn's process tree; the
session stays resumable), end (no more turns). A call refused by Claude Code's own `auto` mode that no
approval rule covers shows in the turn's report as a refusal (tool, input, reason), not as an approval
request: approvals are only for the actions the policy names. Between turns no `claude` process runs, so an idle run
holds no memory and no slot.

**Limits.** Slots per machine (setting `max_running_turns`, default 2) and a free-memory check
before each turn (setting `min_free_memory_mb`, default 1500: on a 16 GB machine with a desktop app
and a few sessions open, 1.8 to 2.4 GB is usually free). A run takes one slot while a turn runs,
whatever the number of its worktrees; builds in two repositories in one turn are what the memory
check catches. Too little memory or no free slot: the turn is **queued with its reason** on the card
and in the Inbox ("waiting for a slot: 2 of 2 running", "waiting for memory: 1.2 GB free, 1.5 GB
needed") with a **Start anyway** button, never refused silently. The brake and the budgets of 0018 act on runs: a
braked run takes no new turn, and the current one is killed after its next refused tool call.

**Lifecycle.** A card moved to done ends its run and removes each worktree the run created (`git
worktree remove`, never one it did not create). A worktree with uncommitted changes or commits not on
any remote is kept, listed on the card, and a question asks the owner whether to remove it.
Decided by the coordinator on 2026-11-12: files git ignores (`node_modules`, `dist`, caches) go
with the worktree without a question; the worktree is kept and the owner asked only when the copied
Claude files changed (`CLAUDE.md`, `CLAUDE.local.md`, `.claude/` including new files in it,
`.mcp.json`, the `.worktreeinclude` files) or there are untracked files git does not ignore,
uncommitted changes or commits on no remote. `run_event` is kept by the project's retention
policy; a run marked "saw a secret" or one that touched a live environment has its events and its
Claude Code transcript deleted when the card closes.

**Visual acceptance in 0.9.** A headless turn has no browser pane of the owner's. When a task needs
the owner to look at a screen, the agent brings up the stand (a local server, a test deployment),
then asks a **"look at this"** question ([0022](0022-questions-and-approvals.md#look-at-this)):
blocking, with the URL, what to check and which test account to use (by name; never a password in a
question). The owner opens it in his own browser and answers on the board; remarks go back to the
agent as the answer. The agent may check the screen itself first with the browser through
`--chrome`, when the project's profile turns it on (`-p --chrome` connects the browser extension's
server, [0012](0012-runner-and-embedded-terminal.md#verify-before-start)). Step-by-step acceptance as
a conversation with the agent comes with the terminal in 0.10.

**Reports.** Each turn's final report lands on the card in full; subagent work shows as nested events
under it. A claim may carry `verified: <where>`; an M or L report without any marks gets a rules
warning.

## Login check

relaybay never signs in for the user and never touches credentials. It only asks the user's own
`claude`: `claude auth status` (documented in the CLI reference, Claude Code 2.1.268 and later)
prints JSON and exits 0 when signed in, 1 when not. Its fields include `loggedIn`, `authMethod` (one
of `none`, `claude.ai`, `oauth_token`, `api_key`, `api_key_helper`, `third_party`), `apiProvider`,
`subscriptionType` and `configDirectory`. relaybay reads only `loggedIn`, `authMethod` and
`subscriptionType`; it never stores or shows the e-mail or organization fields of that answer.
Probed on 2026-11-10 ([0012](0012-runner-and-embedded-terminal.md#probes-2026-11), probes 7 and 11):
a signed-in subscription gives exit 0, `loggedIn` true, `authMethod` `claude.ai`; signed out, exit 1,
`loggedIn` false, `authMethod` `none`; with an API key in the environment, exit 0, `loggedIn` true,
`authMethod` `api_key`. So the API-key case is told apart by `authMethod`, never by the exit code.

| `claude auth status` says | Line in `relaybay doctor` and on the card before Start | Start |
|---|---|---|
| `loggedIn`, `authMethod` `claude.ai` | "claude: signed in with a subscription" | starts |
| exit 1 or `loggedIn` false | "claude: not signed in" | no turn |
| `authMethod` `api_key` or `api_key_helper` | "claude: signed in with an API key (usage is billed to the key)" | no turn, unless the owner allowed API keys in Settings |
| `authMethod` `third_party` | "claude: third-party provider" | no turn, unless allowed in Settings |
| `oauth_token`, or an answer relaybay cannot read | the method as reported, "not a subscription sign-in" | no turn, unless allowed in Settings |

Subscription only is the default; API keys and other methods start runs only when the owner turns
that on in Settings (cookie and CSRF). When Start refuses, it shows one command for a terminal
(`claude`, then `/login` inside it) and why. The check runs before the first turn of a run and again
before a turn after a restart of the program; a failed check is a visible state on the card, never a
silent wait.

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
- **One approval, one call that ran.** An approved call is answered `allow` and its approval is
  reserved for that call (its `tool_use_id`), not spent: its `PostToolUse` (or its result in the
  run's stream) marks it used. A call that did not run, refused by Claude Code after the allow or
  because the answer came too late, releases it with no new request, so neither a lost answer nor
  a classifier makes the owner approve twice, and no refusal opens a loop of requests; the third
  release expires it ("released 3 times"; the agent's report says "approval exhausted, a new
  request is needed"). The check answers within the hook client's 5 s wait for such a call (git
  1.5 s per read, 3 s in all); git too slow denies "could not tell the branch, run it again"
  without reserving ([0022](0022-questions-and-approvals.md#reserved-used-released)).
- **Secrets.** Not in relaybay's database, with one exception that has to be decided: `run_event`
  holds what the agent's tools printed, and a tool can print a secret. Mitigation: the journal's
  secret masking is applied to every stored line, the "saw a secret" mark deletes on close, and the
  retention policy can turn storage off ([question C3](#questions-for-the-coordinator)).
- **On the owner's machine the program may**: start `claude` from `PATH` in a project's folder, one
  of its repositories or a worktree of one; run `git worktree add`, and `git worktree remove` for
  the worktrees a run created (by the owner's answer when work is left in one); copy a
  repository's untracked Claude Code files into a worktree it created ([C14](#questions-for-the-coordinator)); kill the
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

**The rule, decided 2026-11-13 (built for 0.10).** An open Terminal tab is the run "with the
owner" of PR 1c (`with_owner_in = 'terminal'`): no turn starts, texts queue, and closing the tab
(or the TUI exiting) kills the process and then hands the run back like Back to the program. Do
not weaken it: two writers on one Claude Code session fork its transcript; a headless turn run
while the TUI was open passed and was later lost from the session's history (probe on Windows,
claude 2.1.323). Tests hold it: no turn spawns while a terminal is open. The ConPTY probe
rendered cleanly in Chromium and in the pywebview window; macOS and Linux (stdlib `pty`) are
built but not probed.

**Probes.** The probes of [0012](0012-runner-and-embedded-terminal.md#probes-2026-11) answered
the headless questions on Windows. Still open, for 0.10: interactive `claude --resume` in a PTY while
a headless turn runs, rendering of the TUI in each system web view, and the same probes on macOS and
Linux.

## Questions for the owner

Only these stay with the owner; everything else the coordinator decides.

| # | Question | Default | Why the owner |
|---|---|---|---|
| O1 | The terms allow signing in to the unmodified binary with one's own subscription and forbid third parties routing requests through plan credentials or collecting them; programmatic `-p` turns started by a program are a grey area (probe 5 of 0012). Go ahead? | **Decided 2026-11-10: go ahead.** Runs only on the owner's own machine under his own login, relaybay never signs in or touches credentials ([Login check](#login-check)), and the README says so | Terms of the owner's account |
| O2 | Anything that sends data outside the machine (none planned) | Nothing leaves the machine | Data leaving the machine |
| O3 | README promises and the licence (none change) | Unchanged | Public promises |

## Questions for the coordinator

| # | Question | Default | Reason |
|---|---|---|---|
| C1 | First slice | Headless step-by-step runner with questions, approvals, limits and lifecycle in 0.9; terminal in 0.10 | Structured events and the fake already exist; the terminal adds nothing without questions and approvals |
| C2 | Default permission mode for a run | `auto`, with explicit `--allowedTools`; bypass only by the owner's word per card | Probe 3 passed in `auto`. In Manual (the `-p` default) a call needing approval is denied and the turn ends normally (probe 2); a denial becomes an approval request either way |
| C3 | Keep `run_event` in the database | **Decided by the coordinator:** yes, masked with the journal's masking; deleted on close for "saw a secret" or live runs and retention `keep 30 days` per project come with PR 5 | Full reports and nested subagent events need it; the masking is the journal's |
| C4 | Slots and memory | 2 turns at once; 1500 MB free before a turn, both settings; below it the turn queues with its reason and a Start anyway button | Two heavy builds froze a 16 GB machine; 1.8 to 2.4 GB is usually free there, so 3000 would queue everything |
| C5 | `psutil` for free memory | No; platform code (`GlobalMemoryStatusEx`, `sysctl`, `/proc/meminfo`) | One dependency less in the frozen build |
| C6 | Default model and effort | Opus, medium; the other model as a card field set by the coordinator or the owner | Today's practice |
| C7 | One coordinator for all projects | Yes ([0023](0023-coordinator-and-schedules.md)) | Links and priorities across projects are visible only from above |
| C8 | Managers layer | Not built; decided after a week of coordinator work | A layer of retelling is the problem being removed |
| C9 | ACP adapters and containers | Not before the runner has run a week (0017) | Same as 0017 |
| C13 | Sign-in methods a run accepts | Subscription only; API keys and others by the owner's switch in Settings | The owner runs on his subscription; a key bills elsewhere |
| C11 | relaybay's hooks in a run | The user's settings when `setup` installed them there, else the byte-identical command with `--settings` for the run | Claude Code runs an identical handler once (probe 8), so a wrong check fires nothing twice; no dedupe state on relaybay's hook path |
| C12 | Visual acceptance in 0.9 | A blocking "look at this" question with the URL and the test account's name; `--chrome` by the project's profile | No browser pane in a headless turn; the conversation comes with the terminal in 0.10 |
| C14 | A repository's `CLAUDE.md` kept out of git, in a new worktree (Q3 of the PR 1 plan) | **Decided 2026-11-12 by the coordinator:** when the runner creates a worktree it copies from the source repository `CLAUDE.md`, `CLAUDE.local.md`, `.claude/` and `.mcp.json` when present and not tracked by git; a repository with `.worktreeinclude` gives the list instead, as Claude Code does. Local copies only, never `git add`; the Start form shows in one line what is copied | A fresh worktree lacks the untracked files, so the project's rules would not load in it; copying them is a local file copy, no git write |
| C10 | Approval delivery: a new turn, or approval inside a turn | A new turn with the owner's words (probe 3: the agent pushed). `--permission-prompt-tool` (probe 10: works in `-p`) is the upgrade for an approval that must not end the turn ([0022](0022-questions-and-approvals.md#inside-a-turn)) | A turn is the owner's word to the agent; holding a turn open while the owner decides needs the MCP timeout checked first |

## Releases

| Release | Content | Acceptance |
|---|---|---|
| 0.9 | The shutdown fix; the runner on a card (headless, `--resume`, several repositories); questions, "look at this" and approvals as objects; slots and memory; lifecycle; full reports with `verified` marks | One real task of size S or M over two repositories (a backend and a frontend change) from card to push without the desktop chat; the owner answers one question, looks at one screen and gives the approvals on the board |
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
| 1 | The run: the login check before Start (`claude auth status`, the doctor line, the Settings switch for API keys), task defaults on the project (model, effort, profile, `--chrome`), `run`, `run_worktree` and `run_turn` tables, the runner (cwd a project folder or a repository, worktrees per touched repository with `--add-dir`, first turn, `--resume`, stop, end, process tree), the user layer loaded and relaybay's hooks from one source, hooks link turns by `RELAYBAY_RUN_ID` in the environment, the Agent block on the card, `run_start` / `run_status` over MCP | 0030 `project_run_defaults`, 0031 `run`, 0032 `run_worktree` (run, repository, path, branch, created by the run, removed at), 0033 `run_turn` | L (4.5) |
| 1c | Open a run's session in the Claude desktop app (`claude --desktop --resume`, probe 14 of 0012) with the run held "with the owner" and **Back to the program**; permission mode, model and effort between turns (the owner any, a coordinator only lower with MCP `run_settings`), journaled | 0034 `run_with_owner` (columns on `run`) | S (1) |
| 2 | `run_event` stored masked, the full turn report on the card with nested subagent events and refusals of `auto` mode, `verified: <where>` marks and the rules warning | 0035 `run_event`, 0036 `report_check` | M (2) |
| 3 | Questions as objects ([0022](0022-questions-and-approvals.md)): `question_ask` with the "look at this" kind, the Inbox, the answer as a new turn, defaults by deadline | 0037 `question` | M (2) |
| 4 | Approvals as objects ([0022](0022-questions-and-approvals.md)): `approval_request`, policy rules, the guard check, the decision as a new turn | 0039 `approval` (0038 went to the manual archive), policy keys (data, no migration) | M (2.5) |
| 5 | Slots and memory as settings, queueing with a reason and Start anyway, the brake and budgets on runs, lifecycle (worktree removal) and retention | 0040 `run_lifecycle` (`secret_seen_at`, `touched_live_at`, `transcript_path`, `deleted_at`, `project.run_retention`) | M (2) |
| 6 | Docs, SPEC, CHANGELOG, release | none | S |

Numbers follow 0029 and move if other migrations land first.

**Probes.** All thirteen of [0012](0012-runner-and-embedded-terminal.md#probes-2026-11) were run on
2026-11-10 on Windows; the design above relies on them. Worktrees outside the cwd with `--add-dir`
passed on 2026-11-12 (probe 9 of 0012, re-run). Not probed yet: the MCP client timeout of a held
permission prompt, and macOS and Linux.

**Tests with the fake `claude`** (`tests/e2e`; the fake already takes `-p`, `--resume`,
`--permission-mode` and `--allowedTools`):

| PR | Scenario |
|---|---|
| 0 | Stop the server with six wake-up polls open: it stops at once, the polls answer `204`, no 500s |
| 1 | Start a run: the first turn's `system/init` gives the session id; a second turn passes `--resume` with it; the hooks link both turns to the same session row by `RELAYBAY_RUN_ID`; Stop kills the turn's process tree; a second open run on the card or on any of its worktrees is refused; a text sent during a turn waits for it to end (never two turns on one session); a turn's `result.usage` reaches the card. The login check, with a fake `auth status` answer per case: a subscription starts; signed out shows the `/login` command and starts nothing; an API key starts nothing until Settings allow it, then starts; a third-party provider starts nothing; the e-mail and organization fields of the answer appear nowhere on the board or in the database. A run in a project folder over two repositories creates two worktrees and passes both with `--add-dir`. A rule from the user `CLAUDE.md` in the fake's config folder is visible in the turn (the fake gains user-layer loading). With relaybay's hooks in the user's settings the run adds no `--settings` and each hook fires once; without them the run adds `--settings` and each still fires once |
| 2 | A turn with a subagent: the report lands in full, nested events under it; a line with a known token shape is stored masked; an M report without marks gets the warning |
| 3 | A "look at this" question carries the URL and the test account's name, blocks the run, and a password in its text is refused. The fake asks a blocking question and ends its turn; the run shows `waiting`; the owner answers on the board; the next turn's prompt holds the answer verbatim with "owner's answer to question N"; a non-blocking question past its deadline closes with the default, visibly |
| 4 | In Manual mode the fake's `git push` ends in `permission_denials` and an approval request opens from the denial. In `auto`, the fake runs `git push` without an approval: denied by the guard, a request opens; the owner approves; the next turn passes and the push goes through the guard; a second push is denied again; a coordinator's message saying "approved" changes nothing; a denial by `auto` mode of a call no approval rule covers shows as a refusal in the report and opens no request |
| 5 | Two runs hold both slots: a third turn is queued with "waiting for a slot"; a fake memory reader below `min_free_memory_mb` queues with "waiting for memory" and Start anyway starts it; a project brake kills the running turn; a card to done ends the run, removes both worktrees it created, keeps one with uncommitted work and asks; with "saw a secret", deletes its events and transcript |

**Acceptance task.** One real task of size S or M over two repositories, a backend and a frontend
change, from a card to approved pushes, with no desktop chat opened: the card with its fields, Start
in the project folder with two worktrees, the agent works (the owner's common rules and skills
loaded) and asks one question, the owner answers on the board, the agent brings up the stand and asks
"look at this", the owner looks in his own browser and answers, the agent asks to push each branch,
the owner approves on the board, the pushes pass the guard, the report lands in full with its marks,
the card closes and both worktrees are removed. Whatever needed the desktop chat on the way is a
defect of 0.9.

## What we do not build

- A chat of our own; the terminal with the ordinary `claude` is the direct conversation (0.10).
- An Agent SDK runner on a subscription.
- Architect, developer and reviewer as relaybay entities; a Managers page (unless 0.12 says so).
- ACP adapters and containers before the runner has run a week.
- Electron or a browser panel of our own; the diff and browser plugins (owner, 2026-11-10: not now).

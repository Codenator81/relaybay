# 0023 — Coordinator, schedules and profiles

- Status: **Accepted**, 2026-11-16, for 0.11 (draft 1: 2026-11-10; draft 2 accepted by the
  coordinator with the defaults of the [decided points](#decided-points)). Part of
  [0021](0021-agents-inside.md); builds on the runner of [0012](0012-runner-and-embedded-terminal.md),
  questions and approvals of [0022](0022-questions-and-approvals.md), budgets of
  [0018](0018-brake-and-budgets.md) and messages of [0019](0019-session-messages.md). Fills the
  Coordinator and Schedules pages planned in [0004](0004-navigation.md).
- Owner's decisions of 2026-11-16: **no project managers and no Managers page**, at all (a layer of
  retelling is the problem being removed); **editing the permission profiles on the board moves into
  0.11**, since the profiles appear there as data anyway. There is no 0.12 in its earlier form.
- Size: about 10 agent-days in six pull requests ([plan](#plan-by-pull-request)). Migrations from
  0043.

## Problem

Today the coordinator is a desktop session the owner keeps open: it takes intake, writes briefs,
starts sessions, relays answers and approvals, and reads reports. Since 0.9 the program does the
relaying, routing and card keeping. What is left needs a model: intake from chats and "what did the
customer mean", the meaning of a task, checking a report against its sources, retrospectives. That
coordinator moves inside relaybay as one permanent run, woken by what happens on the board.

## Probes 2026-11-16

Answers needed before this design, marked **docs** and **probe** as in
[0012](0012-runner-and-embedded-terminal.md#probes-2026-11). One run of
`scripts/probe_coordinator.py` (opt-in, refuses under `CI`) on Windows, Claude Code 2.1.323, model
haiku, temporary folders, hooks off and MCP servers off, so no board was reached; the user's own
`~/.claude/CLAUDE.md` was loaded as in a real run.

| # | Probe | docs | probe | For the design |
|---|---|---|---|---|
| 15 | `CLAUDE.md` of a folder nested in a git repository | [Memory](https://code.claude.com/docs/en/memory): files are read from the cwd up to the root; auto memory is per git repository (probe 13) | **The repository's file loads too.** A folder `repo/.data/coordinator/` with its own `CLAUDE.md` inside a git repository with another: the agent named both code words; the same folder outside any repository: only its own. With `--settings '{"claudeMdExcludes": ["<repo>/CLAUDE.md"]}'` only its own | A data folder kept inside a git checkout (a developer's setup) would give the coordinator that repository's rules and its auto memory. The coordinator's folder must not be inside a git repository ([folder](#the-folder)) |
| 16 | Profiles: tools taken away | [CLI](https://code.claude.com/docs/en/cli-reference): `--disallowedTools` removes tools from the model's context; `dontAsk` denies whatever is not allowed | `auto` with `--disallowedTools Write,Edit,Bash,NotebookEdit`: the tools are gone from `system/init`, no file made; the model then tried a `Task` subagent told to "use PowerShell instead", and `auto` mode denied that call (`permission_denials`). `dontAsk` with `--allowedTools Read,Glob,Grep`: `Write` and `Bash` denied, no file made, the turn ended `success` | A read-only profile is `dontAsk` plus an allow list, and holds. A list of taken-away tools in `auto` mode is not a wall on its own: whether a subagent inherits `--disallowedTools` was not shown (the denial came from `auto`). The coordinator's profile is an allow list, not a deny list |
| 17 | Claude Code's own scheduling tools | | `system/init` of a `-p` turn lists `CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger`, `PushNotification` | A timer made inside a `-p` turn dies with the process or schedules outside relaybay. Every built-in profile takes these away; schedules are relaybay's own |
| 18 | `/compact` as a headless turn | | `claude -p "/compact" --resume <id>`: a `compact_boundary` event (`trigger: manual`, 51 514 tokens before, 9 589 after, 6 s), an empty `result` with zero usage, **the same session id**; the next turn still knew what the first turn said | relaybay can compact the coordinator's session itself between wakes, without a new session |
| 19 | The cost of a wake | | A one-line wake on a resumed session of two short turns: 2 input, 328 cache write, **51 180 cache read**, 4 output tokens. After the compaction the next turn read 32 393 from cache | A wake costs about the whole context as cache reads. The coordinator's context size, not the number of events, sets the price of a wake: keep it compacted ([context](#context-and-compaction)) |
| 20 | A one-shot check | `--no-session-persistence`, `--max-turns` (print mode) | `--no-session-persistence`: the turn worked, and a later `--resume` of its id answered "No conversation found". `--max-turns 1` on a two-step task: `result` subtype `error_max_turns`, `is_error` true | A read-only check leaves no transcript and is capped in steps; `error_max_turns` is reported as "check stopped at its step limit" |
| 21 | Edit and Write inside one folder only (PR 2, Windows, Claude Code 2.1.323, haiku, hooks and MCP off) | [Permissions](https://code.claude.com/docs/en/permissions): `Edit` rules use gitignore patterns, `//path` is absolute from the root, Windows paths match in POSIX form (`C:\x` as `/c/x`); an `Edit` rule covers `Write` | `dontAsk` with `--allowedTools "Edit(//c/<folder>/**)"`: a `Write` inside the folder made the file; a `Write` to a sibling folder was in `permission_denials` and made nothing; the turn ended `success` | The coordinator's turns add `Edit(//<folder>/**)` to its profile's allow list; nothing else may write |

Not probed: whether a subagent inherits `--disallowedTools`; a long wait of a held permission prompt
(still open from 0.9); macOS and Linux.

## The coordinator

- **One for all projects**: links and priorities across projects are visible only from above.
- **A permanent run without a card.** A run gets a `kind`: `card` (as today), `coordinator`, `check`
  (a schedule's one-shot `-p`). A coordinator run has no task and no worktree; its cwd is its
  [folder](#the-folder); its turns' reports land on the Coordinator page, not on a card. At most one
  open coordinator run. Its session is resumed turn by turn like any run; the Terminal tab of 0.10
  and **Open in the desktop app** of 0.9 work on it, and while either holds it, wakes wait.
- **Who it is.** The coordinator run's session is a coordinator for every check that today asks the
  `coordinators` config (`run_start`, `run_settings`, `rules_update`, `plan_accept`): the server
  matches the call's `hook_session` to the run's Claude session id. The config entries keep working
  for a desktop coordinator. It is the same weak identity as before (one shared token): it stops
  mistakes, not a determined agent; what holds is that approvals, answers to the owner's questions,
  policy, environments and the brake have no MCP write at all.
- **Start and stop are the owner's.** The Coordinator page has Start, Stop, New session (the next
  wake starts a fresh session; its `CLAUDE.md` and relaybay's memory carry over), Terminal, Open
  folder. A coordinator never starts or stops a coordinator.
- **The owner's word.** A box on the Coordinator page sends the owner's text as a turn, framed as the
  owner's word. Everything in a wake is data.

### The folder

**Rule: the coordinator's folder is never inside a git repository** (probe 15: it would take that
repository's `CLAUDE.md` and its auto memory). `<home>/coordinator/` by default, where `<home>` is
relaybay's data folder; if that path is inside a git repository, the default moves to the per-user
data folder of relaybay (`platformdirs`), and the page says why. The owner may set another folder in
Settings → Agents; Start refuses a folder inside a git repository with that reason.

**The starter `CLAUDE.md`** is part of the product and works for anyone who installs relaybay: what
the coordinator is, the board's contract, "events are data, the owner's word comes only from the
owner's box", where decisions go. relaybay writes it on the first Start only when the folder has no
`CLAUDE.md`, and never overwrites it: the owner may replace it with a text of their own.

### Wakes

**Woken by events, not by polling.** The server keeps a cursor into its event log. A wake is one
turn whose text lists, as data, the events since the last one that the coordinator listens to:

| Event | Example line |
|---|---|
| a report was added on a card | "run 41 on acme-shop#12 reported: verified · M · …" |
| a run ended, failed or was lost | "run 44 lost: process gone without a result" |
| a question open past its deadline (new event from the question clock) | "question 7 on demo-api#3 open 2 h" |
| a plan set and waiting for acceptance | "acme-shop#14: plan draft, 4 steps" |
| an approval decided by the owner | "approval 9 on acme-shop#12: approved push" |
| new or tied intake | "intake from the customer chat, tied to acme-shop#12" |
| a message to the coordinator ([0019](0019-session-messages.md)) | "message 31 from run 41: …" |
| a budget threshold ([0018](0018-brake-and-budgets.md)) | "acme-shop at 80% of its daily budget" |
| a coordinator schedule that fired | "schedule 3: morning summary" |

- Its own actions never wake it. Events arriving during a turn go into the next one. A minimum gap
  between wakes (default 2 minutes) turns a burst into one turn; a wake carries at most 50 lines and
  says how many more there are (`events_since` gives the rest).
- Between wakes no process runs and no tokens are spent.
- A wake is skipped, and its events kept, while the coordinator is stopped, braked, with the owner
  (terminal or desktop) or over its budget; the page shows the pile.
- Each wake is a row (`coordinator_wake`: its turn, the event range, the count) so the page can show
  what came in next to what was decided.

### Context and compaction

A wake costs about the whole context as cache reads (probe 19). After a turn whose context (input
plus cache read plus cache write of its `result.usage`) passes `coordinator.compact_at_tokens`
(default 120 000), relaybay sends `/compact` as the next turn before any wake (probe 18). Claude
Code's own automatic compaction stays as the last resort. New session is the owner's button; no
rotation by the clock.

### Budget

The coordinator's run has its own 0018 budget (scope: its session), `warn` at 80% by default; the
warning shows in the Coordinator page's header, not only in the log and the Inbox. A `stop` budget
is the owner's choice once a measured week shows a normal day; until then the page shows the day's
tokens next to the wakes that spent them.

## Memory

Two kinds, each with its own job.

| | Claude Code's own memory | relaybay's memory |
|---|---|---|
| What | `CLAUDE.md` in the agent's folder and auto memory | notes, blocks, decisions and documents in the database ([0008](0008-memory-and-context.md), [0011](0011-docs-in-database.md)), and the project's files |
| Who reads it | that agent only | every session, a replacement session, the owner on the board |
| Works without relaybay | yes | no |

- **The coordinator** keeps both: its own `CLAUDE.md` and auto memory in its folder (which is why the
  folder is outside any git repository, probe 15), and relaybay's memory.
- **The rule.** What someone else or a replacement session must know (decisions, project rules,
  summaries) goes to relaybay's memory or a project file. The personal memory keeps only working
  style, drafts and a pointer to where the rest lives. A decision is never kept twice. No secrets in
  either.
- **A run on a card starts without memory of its own**: every task gets a fresh agent without a tail
  from the last one. It works from the project's `CLAUDE.md`, the card and the decisions in its brief.
- **Where auto memory lives** (probe 13 of [0012](0012-runner-and-embedded-terminal.md#probes-2026-11)):
  per git repository, shared by all its worktrees; a project folder that is not a git repository has
  its own. A project that wants its runs without a tail at all sets `autoMemoryEnabled: false`;
  relaybay suggests it on the project page and never changes it itself.

## Profiles as data

A profile is a stored row, edited by the owner on the board (Settings → Profiles) and read at the
start of every turn: a change takes effect on the next turn of every run that uses it. Each turn
records the profile's name and version, so a report can be traced to the rights it ran with.

| Field | Meaning |
|---|---|
| name, description | `agent`, `coordinator`, `read-only` ship; the owner may add more (`agent-strict`) |
| permission mode | one of the CLI's modes except `bypassPermissions`: that stays the owner's choice for one run on the Start form (0.9), and a profile edit leaves it as it is |
| allowed tools | `--allowedTools` rules, one per line |
| taken-away tools | `--disallowedTools` rules, one per line |
| board rights | what a run under it may do on the board, checkboxes below |
| for kinds | which run kinds may use it: `card`, `coordinator`, `check` |

| Board right | `agent` | `coordinator` | `read-only` |
|---|---|---|---|
| read the board | yes | yes | no MCP |
| write its own card (reports, plan, questions, approval requests) | yes | its own page | no |
| `run_start` on cards in `plan` or `dev`, within slots and budgets | no | yes, at most 10 a day by default | no |
| `run_settings` (lower only) | no | yes | no |
| create and update tasks, triage the backlog, intake | no | yes | no |
| `plan_accept` | no | yes | no |
| `rules_update` | no | yes | no |
| `schedule_add` (one-off checks and reminders) | no | yes | no |
| messages to agents | to its coordinator | yes | no |

The shipped rows:

| | `agent` | `coordinator` | `read-only` |
|---|---|---|---|
| mode | `auto` | `dontAsk` | `dontAsk` |
| allowed | `mcp__relaybay` | `Read`, `Glob`, `Grep`, `Task`, `WebFetch`, `WebSearch`, `mcp__relaybay`, `Edit` and `Write` inside its folder, read-only `git` and `gh` commands | `Read`, `Glob`, `Grep`, read-only `git` and `gh` commands |
| taken away | `CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger` (probe 17) | the same | the same |

**Not editable**, whatever a profile says: an approval is a board button of the owner; nothing
answers the owner's questions for the owner; policy, environments, levels and the brake release have
no MCP write; live always needs an approval; relaybay never passes `--bare` or a skip of
permissions; a coordinator never starts a coordinator; Claude Code's own schedule tools
(`CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger`) are taken away in every profile,
because schedules are the program's, not the agent's (probe 17). The editor shows these lines as fixed.

**Where profiles are used.** The project's run defaults (0030) name a profile instead of their own
mode and tools; the migration turns each project's current pair into a profile `agent-<project>`
when it differs from `agent`, so no run changes its rights by the upgrade. The Start form of a card
offers the `card` profiles. Edits are journaled (Audit) with a before and after.

## Schedules

A clock in the program, kept in `schedule` (kind, when, target, prompt, created by, enabled, next
time) and `schedule_run` (started, ended, outcome, tokens, its run turn or report).

| When | Form |
|---|---|
| once | at a date and time |
| daily | at a time, optionally weekdays only |
| every N minutes | N at least 5, at most 24 times, or until the check's answer starts with `done` |

| Kind | Example | Runs as |
|---|---|---|
| a coordinator turn | "morning summary at 08:30" | one turn of the coordinator's run with the schedule's text, as a wake line |
| a read-only check | "check CI of PR 51 every 10 minutes" | a `check` run: one-shot `claude -p` without resume, `--no-session-persistence`, `--max-turns` 8, profile `read-only`, in the task's worktree or project folder (probe 20); its answer becomes a report on the card, first line `inspected · S · …` |
| a reminder | "remind me of question 7 at 14:00" | the server alone: a tray notification and an Inbox line, no model |

- Times are the machine's local time; the program asleep or closed at the time: on the next start a
  run missed by less than an hour runs once, older ones are logged as missed and the next time is
  planned.
- The coordinator may create one-off checks and reminders over MCP (`schedule_add`), and checks every
  N minutes bounded by `max_times` (K8), list and cancel its own (`schedule_list`,
  `schedule_cancel`); daily schedules, repeating reminders and coordinator turns are made on the
  board. A check counts in the slots and the budgets and queues for them like a run. At most one
  run of a schedule at a time.

## The Coordinator page

- **Header:** status, session, context size, today's tokens against the budget, Start, Stop, New
  session, Terminal, Open folder, the owner's box.
- **What came in:** the wakes, each with its event lines.
- **What it decided and why:** its turn reports beside the wake that caused them.
- **What waits for the owner:** its open questions and the approvals of the runs it started.
- **Runs it started today**, and **its schedules** with the next time.

## Risks

| Risk | Mitigation |
|---|---|
| The coordinator spends tokens on noise | Event wakes only, a minimum gap, compaction (probes 18, 19), its own budget |
| It starts too many runs | Slots, budgets, `plan` and `dev` only, a daily cap in its profile, the brake |
| Its folder picks up a repository's rules and memory | The folder is outside any git repository; Start refuses one inside (probe 15) |
| A profile edit widens rights by mistake | Owner only, journaled with before and after, fixed lines shown, effect on the next turn visible in the turn |
| A one-shot `-p` under "bare by default" loses hooks and the login | The 0012 watch item; the contract test fails loudly |
| The coordinator's message is read as the owner's | The frame of 0019; approvals look only at `approval` rows; only the owner's box carries the owner's word |
| A schedule fires into a stopped or braked coordinator | The wake waits with its events; the page shows the pile |

## Tests with the fake `claude`

A report added wakes the coordinator with one turn listing it; two reports during a turn make one next
turn; its own report wakes nothing; a wake while the terminal holds it waits; a context over
`compact_at_tokens` sends `/compact` before the next wake; `run_start` from the coordinator on a card
in `review` is refused, and the eleventh of a day too; a profile edit shows on the next turn's
`--permission-mode` and `--allowedTools`; a reminder fires on a fake clock without starting `claude`;
a read-only check starts the fake with `-p`, no `--resume`, `--no-session-persistence`,
`--max-turns`, the read-only tools, and writes a report; a missed schedule runs once after a restart
within the hour and is logged as missed after it; Start in a folder inside a git repository is
refused.

## Decided points

Decided by the coordinator on 2026-11-16, each as proposed.

| # | Point | Decision | Why |
|---|---|---|---|
| K1 | A run without a card | `run.kind` with `task_id` nullable (table rebuilds of `run` and `question`) | A hidden service project and card would leak into the board, digests, budgets and intake; a kind is one place to check |
| K2 | The coordinator's permission mode | `dontAsk` with an allow list | Probe 16: an allow list holds, a deny list in `auto` did not show it holds for subagents; the coordinator writes on the board, not in repositories |
| K3 | Compaction | relaybay sends `/compact` above 120 000 context tokens; New session only by the owner | Probe 19: a wake costs its context; probe 18: compaction keeps the session |
| K4 | The coordinator's budget | `warn` at 80% of a measured day; `stop` by the owner after a week | No normal day is measured yet |
| K5 | The agents view (who started whom, who writes to whom) of the earlier 0.11 line | Not in 0.11; the Coordinator page lists the runs it started | The Map was removed in 0.8.2; one page less until a week of runs shows the need |
| K6 | Missed schedules | Run once if missed by under an hour, else logged as missed | A morning summary at noon is noise; a CI check an hour late is stale |
| K7 | The coordinator's daily cap on `run_start` | 10, a profile field | A loop of starts is the costly failure; slots alone allow it over a day |
| K8 | Repeating checks over MCP | Decided by default in PR 4: `schedule_add` takes `every_minutes` for a check, at most `max_times` (24) times; each run of a check counts in `run_start_per_day` (an `every` check each time), and at most 10 of its schedules may still run | "Check CI of PR 51 every 10 minutes" is the design's own example and the coordinator is the one that knows when to watch; the bound, the daily counter and the `done` answer keep it from becoming a loop |

## Plan by pull request

| PR | Content | Migrations | Size |
|---|---|---|---|
| 1 | Profiles as data: `run_profile` with the three shipped rows, project defaults name a profile (each project's own pair kept as `agent-<project>`), every turn reads its profile and records name and version, Settings → Profiles editor with the fixed lines, Audit lines | 0043 `run_profile`, 0044 `project_run_defaults.profile`, `run_turn.profile` | M (2) |
| 2 | The coordinator run: run `kind`, a run without a card, its folder with the starter `CLAUDE.md` and the git-repository check, identity by its session, its questions without a card, Start, Stop, New session, Terminal and the owner's box on a first Coordinator page | 0045 `run.kind` and `question` (table rebuilds: `task_id` nullable for a coordinator run and its questions) | L (2.5) |
| 3 | Wakes: the event cursor, the listened kinds, the overdue-question event, batching and the minimum gap, `coordinator_wake`, compaction at the threshold, the coordinator's budget | 0046 `coordinator_wake` | M (2) |
| 4 | Schedules: the clock with the missed rule, reminders, read-only checks as `check` runs, coordinator turns, MCP `schedule_add` / `schedule_list` / `schedule_cancel`, the Schedules page | 0047 `schedule`, `schedule_run` | M (2) |
| 5 | The Coordinator page complete: what came in, what it decided, what waits, runs it started, its schedules | none | S (1) |
| 6 | Docs, SPEC, CHANGELOG, release 0.11.0 | none | S (0.5) |

Numbers follow 0042 and move if other migrations land first.

## Acceptance (0.11)

A working day in which the coordinator inside relaybay sets and drives the tasks, and the desktop
coordinator only watches; one profile changed on the board takes effect on the next turn of a run.

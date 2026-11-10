# 0023 — Coordinator and schedules

- Status: **Proposed**, 2026-11-10, for 0.11. Part of [0021](0021-agents-inside.md); needs the runner
  of [0012](0012-runner-and-embedded-terminal.md) and [0022](0022-questions-and-approvals.md). Fills
  the Coordinator and Schedules pages planned in [0004](0004-navigation.md).
- Size: about 6 agent-days. Migrations: `run_profile`, `schedule`, `schedule_run`, `coordinator_wake`
  (numbered after 0.9's).

## Problem

Today the coordinator is a desktop session the owner keeps open: it takes intake, writes briefs,
starts sessions, relays answers and approvals, and reads reports. After 0.9 the program does the
relaying, routing and card keeping. What is left needs a model: intake from chats and "what did the
customer mean", the meaning of a task, checking a report against its sources, retrospectives. That
coordinator moves inside relaybay as one permanent run.

## The coordinator

- **One for all projects**: links and priorities across projects are visible only from above.
- **A permanent run** in the program's own folder (under the data folder), with its own `CLAUDE.md`
  written by the owner, profile `coordinator`. Its session is resumed turn by turn like any run; the
  terminal of 0.10 opens it for a direct talk.
- **Woken by events, not by polling.** A wake is one turn whose text lists what happened since the
  last one, as data:

| Event | Example |
|---|---|
| a report was added | "run 41 on acme-shop#12 reported: verified · M · …" |
| a question unanswered past its time | "question 7 on demo-api#3 open 2 h" |
| new intake | "intake from the customer chat, tied to acme-shop#12" |
| a run failed or was lost | "run 44 lost: process gone without a result" |
| a budget threshold | "acme-shop at 80% of its daily budget" |

  Events arriving during a turn are batched into the next one. Between events no process runs and no
  tokens are spent. A minimum gap between wakes (default 2 minutes) keeps a burst from becoming many
  turns.

## Memory

Two kinds, each with its own job.

| | Claude Code's own memory | relaybay's memory |
|---|---|---|
| What | `CLAUDE.md` in the agent's folder and auto memory | notes, blocks, decisions and documents in the database ([0008](0008-memory-and-context.md), [0011](0011-docs-in-database.md)), and the project's files |
| Who reads it | that agent only | every session, a replacement session, the owner on the board |
| Works without relaybay | yes | no |

- **The coordinator** (and a manager, if one is ever built) keeps both: its own `CLAUDE.md` and auto
  memory in its folder, and relaybay's memory.
- **The rule.** What someone else or a replacement session must know (decisions, project rules,
  summaries) goes to relaybay's memory or a project file. The personal memory keeps only working
  style, drafts and a pointer to where the rest lives. A decision is never kept twice. No secrets in
  either.
- **A run on a card starts without memory of its own**: every task gets a fresh agent without a tail
  from the last one. It works from the project's `CLAUDE.md`, the card and the decisions in its brief.
- **Where auto memory lives** (probe 13 of [0012](0012-runner-and-embedded-terminal.md#probes-2026-11)):
  per git repository, shared by all its worktrees, so runs in worktrees of one repository read and
  add to one memory; a project folder that is not a git repository has its own. A project that wants
  its runs without a tail at all sets `autoMemoryEnabled: false` in its settings; relaybay suggests
  it on the project page and never changes it itself.

## Profiles as data

A profile is a stored row: name, permission mode, allowed tools, what it may do on the board. Three
ship: `agent` (a run on a card), `coordinator`, `read-only` (schedules' checks). Editing them on the
board is 0.12.

| The coordinator may | It may not |
|---|---|
| read everything on the board | approve anything |
| `run_start` on cards in `plan` or `dev`, within slots and budgets | answer the owner's questions for the owner |
| triage the backlog, create and update tasks | change the policy, environments or levels |
| `rules_update` (it is in `coordinators`) | release a brake |
| send messages to agents ([0019](0019-session-messages.md)), which reach them as data | start another coordinator |

## Schedules

A clock in the program, kept in `schedule` (kind, when: once at a time, daily at a time, every N
minutes until done or at most 24 times; target project or task; created by; enabled) and
`schedule_run` (started, ended, outcome, tokens).

| Kind | Example | Runs as |
|---|---|---|
| a coordinator turn | "morning summary at 08:30" | one turn of the coordinator's run with the schedule's text |
| a read-only check | "check CI of PR 51 in 15 minutes" | one-shot `claude -p` without resume, profile `read-only`, in the task's worktree; the answer as a report on the card |
| a reminder | "remind me of question 7 at 14:00" | the server alone: a tray notification and an Inbox line, no model |

The coordinator may create one-off checks and reminders over MCP (`schedule_add`); repeating
schedules are made on the board. A check counts in the slots and the budgets. At most one run of a
schedule at a time.

## The Coordinator page

What came in (the wake events), what it decided and why (its turn reports), what waits for the owner
(its open questions and the approvals of the runs it started), the runs it started today, its token
use against its budget.

## Risks

| Risk | Mitigation |
|---|---|
| The coordinator spends tokens on noise | Event wakes only, a minimum gap, its own budget with `stop` |
| It starts too many runs | Slots, budgets, `plan` and `dev` only, the brake |
| A one-shot `-p` under "bare by default" loses hooks and the login | The 0012 watch item; the contract test fails loudly |
| The coordinator's message is read as the owner's | The frame of 0019; approvals look only at `approval` rows |

## Tests with the fake `claude`

A report added wakes the coordinator with one turn listing it; two reports during a turn make one next
turn; a reminder fires on a fake clock without starting `claude`; a read-only check starts the fake
with `-p`, no `--resume`, the read-only tools, and writes a report; `run_start` from the coordinator
on a card in `review` is refused.

## Acceptance (0.11)

A working day in which the coordinator inside relaybay sets and drives the tasks, and the desktop
coordinator only watches.

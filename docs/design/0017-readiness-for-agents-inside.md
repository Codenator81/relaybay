# 0017 — Readiness for agents inside

- Status: **Accepted** (direction), 2026-11-06. What has to be true before relaybay starts and
  steers agents itself ([0012](0012-runner-and-embedded-terminal.md)). Decided by the owner after
  two days on the installed program: what helps the current stage (desktop sessions reporting through
  hooks) is done now; what only the next stage needs waits here.
- Size: the "now" items ~4 agent-days (0.7.x); the "before the runner" items ~6 agent-days (0.8).

## Problem

Today the owner runs many Claude Code sessions in the desktop app and relaybay watches them through
hooks and MCP. The first day on the installed program showed where that picture can go wrong: a
hook event lost silently (a todo update), the program not coming up at login and the board standing
a night without a server, findings fixed without a task that cannot be closed. With agents started by
relaybay those gaps turn from an inconvenience into wrong decisions made on a wrong picture. This
document lists the gaps, in order, and which release closes each.

## Now (0.7.x, desktop sessions)

| Gap | What is done | Design |
|---|---|---|
| A hook event can be lost: the forwarder tries once with a 0.3 s timeout and stays silent | State-bearing events (session start and end, stop, prompts, todo tools) are retried once and spooled on the machine when the server is away; replayed in order on the next call; `doctor` shows the spool | [0001](0001-hooks-and-setup.md) |
| The server dies and nobody restarts it | A watchdog in the program checks its embedded server every 30 s and restarts it, capped; external mode reports | [0016](0016-desktop-program.md) |
| A finding fixed without a task stays "new" on the backlog | A `done` decision with a link, on the board and over MCP for coordinators, in `backlog.md` | [0015](0015-intake-and-autonomy.md) |
| A plan closes while the work goes on | `plan_step_add`, the stage follows the plan, closed-plan warnings, draft hints | [0014](0014-task-plan.md) |
| The program did not start at login once | A start journal and a `desktop start` item in `doctor`; the cause is found from the journal | [0016](0016-desktop-program.md) |

## Before the runner (0.8)

| Gap | What is needed | Why it blocks agents inside |
|---|---|---|
| Sessions talk to each other outside relaybay (the desktop app's messages) | Messages between sessions as a board entity: sender, recipient, task, delivered and read, carried by the same hooks as owner comments; the coordinator role can then live inside | A coordinator inside relaybay has no other channel to its agents |
| No way to stop everything or to cap spending | Stop all, a project, a session or a task at the next tool call; a token budget per task, project or all, per UTC day or in total, warning by default with `stop` opt-in; a warning on concurrent runs. Tokens only: relaybay knows no prices. Done in [0018](0018-brake-and-budgets.md) | Autonomy without a brake is not acceptable to the owner |
| Agent scenarios are checked by hand | A fake `claude` (a script that answers stream-json and fires the hooks) and end-to-end tests task → run → report → review; this also answers 0012's "verify before start" | The runner cannot be changed without fear otherwise |
| Clients cache tool schemas; tools change between releases | A versioned MCP contract: `task_patch`-style escape hatches for every tool, deprecations with dates, a test that the published reference did not change by accident (the generated-reference check exists; the deprecation rule does not) | An agent inside relaybay calls the tools from a fixed build |
| The backlog is a list, not a queue | Priority and "for whom" on backlog items, so a coordinator takes work from the backlog instead of from a chat | The queue is the coordinator's input |
| Actions on servers are journaled but not shown as an audit, and access cannot be revoked at once | An Audit page (who did what on which stand) and a per-agent "no live now" switch on top of the policy. As built in 0.8: [SPEC](../SPEC.md#spec-no-live); per session row, a nested `claude -p` has its own row | Agents inside act on the owner's behalf on real stands |

## Not before the runner has run for a week on the owner's workflow

- Agent Client Protocol adapters and the container runtime ([0012](0012-runner-and-embedded-terminal.md), "Later").
- A board window on Linux ([0016](0016-desktop-program.md), backlog #8): blocks 1.0, not the runner.

## Decided by default

- The order above follows the owner's own workflow: reliability of the picture first, then the brake,
  then the channel, then the queue.
- ~~Budgets at the current stage can only warn (relaybay cannot stop a desktop session); the stop is
  built together with the runner.~~ Out of date: a hook answer ends a session's turn at its next tool
  call ([0018](0018-brake-and-budgets.md)); killing a process tree still needs the runner.
- Nothing here changes the rule that agents are the user's own `claude` under the user's own login.

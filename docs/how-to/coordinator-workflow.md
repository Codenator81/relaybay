# Coordinator and agents workflow

A way to run several Claude Code sessions on one board: one coordinator session that you talk to,
and one agent session per card that does the work. Nothing here is enforced beyond the project's
[working rules](../SPEC.md#spec-rules); it is the shape relaybay is built for. The coordinator's
own tools are in [Coordinate many projects](coordinator.md), an agent's daily loop in
[Work a card with a session](work-a-card.md).

## The card holds the task

Everything a stranger needs to pick the task up lives on its card, not in a chat:

| What | Where on the card | Written with |
|---|---|---|
| The plan: 2-5 steps, accepted before the work starts | Plan | `plan_set`, `plan_step_update`, `plan_accept` (coordinator or owner) |
| What is done so far, in plain words | Progress | `task_update(progress=…)` |
| What happens next and who acts | Next, Waiting on | `task_update(next_step=…, waiting_on=…)` |
| Decisions and their reasons | Decisions | `decision_add` |
| Stage reports (`state · size · gist`) | Reports | `report_add` |
| What a deploy needs: a migration, a flag, a script, the rollback | Deploy notes, with a done mark | `task_update(deploy_notes=…, deploy_done=true)` |
| Cards that must land first | Blocked by (and Blocks) | `task_update(blocked_by=["demo-api-2"])` |
| The repositories, branches, worktrees and pull requests | Code | `task_update(repos=…, prs=…)`, the hooks |
| Why the card was given up on | Cancelled | `task_update(cancelled="…")` |

A card blocked by another shows a `blocked by <key>` chip until the blocker reaches `live` or
`done`. With two or more cards in `test`, the project digest and the board's test column show the
**merge order**: blockers first, then the column's order, with a warning when two test cards share
a repository ([SPEC](../SPEC.md#spec-merge-order)).

## A typical round

1. **You tell the coordinator what you want.** It reads the board (`portfolio_digest`,
   `project_digest`), creates the card (`task_create`) and writes the brief into its description.
2. **The coordinator starts an agent session** for the card. A start prompt that names the card's
   key binds the new session to it ([binding at start](coordinator.md#binding-at-start)).
3. **The agent plans.** It reads `task_context`, writes the plan with `plan_set` and waits; you or
   the coordinator accept it. A move to `dev` without an accepted plan is warned about or refused,
   as the rules say (size `S` needs none).
4. **The agent works and reports.** It marks plan steps done, keeps Progress and Next current and
   adds a report at the end of each stage. Your comments on the card reach the bound session at its
   next turn; its answers come back on the card.
5. **The coordinator keeps the overview.** It catches up with `events_since`, answers what it can,
   brings you what only you can decide, and orders the merges by the merge order.
6. **Close.** The card goes to `done`; the checks below tell the agent what is left.

## Main spec and change documents

The project's main spec (a document such as `spec`) says how the product works now. A card that
changes that behaviour writes its change first as a separate document named `spec-<card key>`
(`spec-acme-shop-12`): one `## spec#<anchor>` heading per section it changes, with `ADDED:`,
`MODIFIED:` (and a `Was:` line quoting the old value) or `REMOVED:` blocks
([format](../SPEC.md#spec-change-documents)).

While the card is open, every section it names carries a mark `changing on <stage>:
<card key>`: on the document page under the heading, in `doc_get` of that section and in
`doc_outline`. An agent reading the spec sees at once that another card is about to change it, and
`task_context` names the other cards that change the same sections. The format is checked softly:
a target that names no section, or a block without an op, comes back as a warning on the write,
never a refusal. When the card is deployed, its change is written into the main spec and the change
document is deleted (`doc_update(delete=true)`); the mark goes with it.

## What closing a card checks

A move to `done` is checked by the project's rules (by default: Progress must be filled; a project
may also ask for a tracker work-log mark) and adds hints that do not stop it:

- the change document `spec-<key>` still exists: merge it into the spec and delete it;
- the deploy notes are not marked done.

A cancelled card stays in its stage until it is moved, and gets neither hint. A done card that
other cards were blocked by stops blocking them.

## Roles and tool sets

The server offers the same tools at four endpoints; each lists a different set in Claude Code
([Connect Claude Code](../connect-claude-code.md#tool-sets-by-role)):

| Endpoint | For | Lists |
|---|---|---|
| `/mcp` | any session, the default | every tool, with the general instructions |
| `/mcp/agent` | a session that works one card | the tools of each moment of a card (the table below) |
| `/mcp/coordinator` | the session you talk to | every current tool but the agent's acceptance work on a card |
| `/mcp/reader` | review and planning subagents | the reads: `task_get`, `task_context`, `plan_get`, `review_get`, the documents |

Give the workers the agent set in your user configuration, and the coordinator's project its own
set with a local-scope entry, which wins in that folder:

```
claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp/agent --header "Authorization: Bearer <token>"
claude mcp add --transport http --scope local relaybay http://127.0.0.1:8765/mcp/coordinator --header "Authorization: Bearer <token>"
```

Claude Code shows a session only the tools its endpoint lists. A role is not a permission: every
endpoint shares the token, and a tool left out of a list still runs when a client calls it by name.
What only a coordinator may do is decided by the `coordinators` setting, not by the endpoint.

## Coordinator mode

Naming the coordinator in the server's config (`coordinators = ["lead"]`, a name, an alias or a
desktop session id) switches on coordinator mode ([SPEC](../SPEC.md#spec-coordinator-mode)). An
**agent** is then a session that is bound to an open card and is not a coordinator; the board, the
coordinator and a session bound to no card are not agents. In this mode:

- **No cards or backlog items from agents.** `task_create` and `backlog_add` from an agent are
  refused with one line saying where the thing goes instead: `report_add(task=<its card>,
  findings=[...])`, a question for the owner as `questions=[...]`. The coordinator triages them.
- **The tracker mark is the coordinator's.** A project rule that asks for the tracker work log
  (`require_tracker_log`) does not hold up an agent's move to `done`, in strict mode either; the
  answer says `Tracker mark: the coordinator's (it sets tracker_logged after the work log).` The
  coordinator sets `tracker_logged` and archives; `task_archive` refuses a done card that still
  needs the mark.
- **`waiting_on` names a fixed role.** On an MCP write it is saved as the owner's first name from
  `owner_names` (so the Inbox finds it), `coordinator`, `session`, `ci` or `customer:<name>`;
  common words (`owner`, `master`, `developer`, `reviewer`, the coordinator's names, …) map to
  them, anything else stays as written. Text after the who (`owner: merge the PR`) moves to
  `next_step` when the card has none. The answer names what was saved.
- **A request nobody needs any more is withdrawn, not left open.** When the owner did the merge by
  hand, the asking agent or the coordinator calls `approval_withdraw(approval, reason)`: the
  request expires with the reason, leaves the Inbox and no longer holds a live card's archive.
- **The instructions say so.** `/mcp` and `/mcp/agent` tell the agent to ask the coordinator for a
  missing card, to put findings and questions into its reports and to leave the tracker mark.

With `coordinators` empty none of this applies: texts and behaviour are as described above.

## A card's life, moment by moment

What an agent calls at each moment, and what the answer points to next:

| Moment | Agent tool | What the answer tells next |
|---|---|---|
| Start | `task_context` (refresh), `task_get` (every field) | the plan state, open comments, the rules line |
| Plan | `plan_set`, `plan_step_update`, `plan_step_add` | that the plan waits for acceptance; which step is open |
| Work | `task_update` (stage, progress, next step), `doc_outline`, `doc_get`, `doc_update` | a `Warning (rules)` line for an unmet rule |
| Question | `report_add(questions=[...])`; working alone: `question_ask` | the question shows to the owner; the answer comes back as a comment |
| Decision | `decision_add` | the decision is on the card and in `task_context` |
| Finding | `report_add(findings=[...])`; working alone: `backlog_add` | where the coordinator sees it |
| Approval | `approval_request`, then end the turn; `approval_withdraw(reason=…)` when it is no longer needed | the decision arrives as an owner comment |
| Report | `report_add` | a format warning, newer rules |
| Review | `review_steps_set`, `attach_file`, `review_step_update` | the owner's marks (`review_get`) |
| Closing | `task_update(stage="done")` | what is left: change document, deploy notes; the coordinator sets `tracker_logged` and archives |

## Sample CLAUDE.md rules

For the agents (user-level `CLAUDE.md`):

```markdown
## Task board (relaybay)
- Your card is in the start prompt (acme-shop-12). Read task_context first; plan with plan_set.
- Keep progress, next_step and waiting_on current on every report (waiting_on: owner,
  coordinator, session, ci).
- Findings outside the card and questions for the owner go into
  report_add(findings=[...], questions=[...]); do not create cards or backlog items.
- Before a push or a deploy: approval_request, then end the turn.
- Done: task_update(stage="done") with the final report; the tracker mark is the coordinator's.
```

For the coordinator (the coordinator's project, `CLAUDE.local.md` or its `CLAUDE.md`):

```markdown
## Coordinating acme-shop and demo-api
- Catch up with portfolio_digest and events_since before answering.
- New work: task_create(bind=false) with an area and a brief, then start a session whose prompt
  names the card key.
- Triage the findings and questions in agents' reports: a new card, a backlog item, or an answer.
- Accept plans with plan_accept; order merges by the merge order of the test column.
- After the work log: task_update(tracker_logged=true), then task_archive.
```

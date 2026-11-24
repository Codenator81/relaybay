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

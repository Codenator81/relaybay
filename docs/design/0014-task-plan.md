# 0014 — Task plan and checkpoints

- Status: **Implemented** (step 11.7, unreleased). Shipped behaviour:
  [SPEC](../SPEC.md#spec-plan); tools: [MCP tools → Plan](../mcp-tools.md#plan).
- Size: ~2 agent-days (model, migration and service ~0.75; hooks, todo and reminders ~0.5; MCP,
  board and docs ~0.75).

## Problem

An agent should always work by a plan the owner can see. A report at the end of a stage says what
happened, not what was meant to happen, and nothing shows a drift while the work goes on. Claude
Code keeps its own todo list, but it lives inside one context, disappears with `/clear` and is not
something the owner accepted.

## Model

- **Plan on the task:** ordered `plan_step` rows (title, "what and why" markdown, status
  `pending` / `in_progress` / `done` / `blocked`). A done step is a **checkpoint**: `done_at`, a
  link (a report of the task or a PR URL), a one-line outcome and a **conformance** line, either
  `as planned` or `deviation: <why>` (required for done). Deviations are highlighted on the card
  and in the Plan tab.
- **Plan status** on the task row: none (no steps), `draft`, `accepted` (with who and when). The
  owner accepts on the board, the coordinator with `plan_accept`. A change of the step titles or
  their order turns an accepted plan back to draft; status changes and "what and why" edits do
  not.
- **History:** every plan write adds a `plan_revision` row with the whole plan as JSON, the
  author and a reason; the board shows them with the diff of the titles.
- **Task size:** an optional `size` field (`S` / `M` / `L`), editable like the other fields; the
  first report whose first line names a size sets it when the task has none.
- **Second layer, never merged:** the agent's Claude Code todo (`session_todo`, per relaybay
  session and Claude Code id) shows under the current plan step as "now doing: …" with the list
  collapsed. A task with no plan steps shows the todo marked "Agent's plan, not accepted".

## Rules

A new requirement kind in the rules document: `{"kind": "plan_accepted", "unless_size": ["S"]}`,
in the default `stage_requirements.dev`. A task of unknown size needs the plan. `warn` mode adds a
warning to the move (a draft plan: "the plan is not accepted yet"); `strict` refuses it. A move to
dev with neither plan steps nor a todo also adds a soft hint: "write down what you are going to do
(todo or 2-3 steps with plan_set)".

## Hooks

- **Todo capture.** Checked live on Claude Code 2.1.309: the todo tools are off by default (desktop
  and CLI); with the env `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` they are `TaskCreate` (tool_input
  `subject`, `description`, `activeForm`; tool_response `{"task": {"id", "subject"}}`) and
  `TaskUpdate` (tool_input `taskId`, `status`; tool_response with `statusChange: {from, to}`).
  Older builds use `TodoWrite` with the whole list (`todos: [{content, status, activeForm}]`).
  relaybay reads all three from `PostToolUse`, before the activity coalescing, so no change is
  lost; unknown shapes are ignored and sizes capped (50 items, 200 characters). A new Claude Code
  id (after `/clear`) starts a fresh list; a list written before the agent linked its hook
  session moves with the exact link.
- **Reminder.** For an exactly linked session (`cc_exact`) and its bound open tasks: the current
  step (N/M, title, "what and why") and the todo item in progress go into `additionalContext` on
  `SessionStart`, after a todo change and every `plan_reminder_every` tool events (default 30),
  never the same text twice in a row (a new `SessionStart` is a new context). Owner comments in
  the same answer keep the rest of the 8000-character budget.

## API

`plan_get(task)`, `plan_set(task, steps, reason?)` (replace semantics like `review_steps_set`:
step N keeps status and checkpoint while its title stays), `plan_step_update(task, step, status,
outcome?, link?, conformance?)`, `plan_accept(task)`; `task_update` gains `size`. Events
`plan.set`, `plan.step_updated`, `plan.accepted`, `session.todo_updated`.

## Decided by default

- relaybay setup does not write `CLAUDE_CODE_ENABLE_TODO_TOOLS`: it is the user's choice and easy
  to add or remove in Claude Code's settings.
- Plan writes that change nothing (same titles and details, same status and checkpoint) are not
  kept as revisions.
- Leaving `done` clears the checkpoint; marking a done step done again (a new outcome or
  conformance) keeps its `done_at`.
- A plan step's text is `details` in `plan_set`, as in `review_steps_set`.
- A link is a report of the same task (`12`, `#12`, `report:12`, stored as `report:12`) or an
  http(s) URL; anything else is refused.
- Agents do not accept plans; `plan_accept` is meant for the coordinator.
- Reminders count `PostToolUse` events only; `0` turns the periodic reminder off.
- The card shows the plan, not the todo; the todo is in the Plan tab.

## Living plan (0.7)

On a real task the plan was 3/3 done in draft while rework went on by the owner's decisions: the
plan could not grow without losing its acceptance and nobody was told to add a step. The plan is a
map of where the work goes and where it is, for the owner and the agent, all the way.

1. **`plan_step_add(task, title, details?, reason)`** appends a pending step. The plan's status
   stays as it is; on an accepted plan the step carries `added_after_acceptance_at`, `added_by` and
   `added_reason` (migration `0022`), shown in the Plan tab and in the history diff as
   `+ 4. title (added after acceptance)`. A `plan_revision` with the reason and `plan.step_added`.
   `reason` is required. `plan_set` keeps its rule: changed titles or order make a draft.
2. **Server instruction 11:** a change or an addition after a report is a new step with
   `plan_step_add` (or the step back to `in_progress`), the stage back to dev, `progress` and
   `next_step` updated.
3. **The stage follows the plan.** `plan_step_add`, and `plan_step_update` taking a done step back
   to `in_progress`, on a task in `review` or `test` move it to `dev` in the same transaction with
   the stage note `rework: <reason>` (a reopened step: `rework: <its title>`) and the usual
   `task.stage_changed`; the answer says `<key> moved from review back to dev (rework: …).`. When a
   `plan_step_update` makes every step done on a task in `dev` that has review steps, the answer
   adds `Hint: all steps done and the review checklist is set: move the task to review
   (task_set_stage).` Nothing moves to review by itself.
4. **Closed-plan warning.** On a task whose plan steps are all done and a report came strictly
   after the plan closed (the newer of the newest `done_at` and the newest plan revision, so a
   `plan_set` dropping the last open step closes it too), `report_add` adds
   `Warning (rules): the plan is closed (N/N done) while work goes on: add a step with
   plan_step_add or reopen one.` when the state is `changed` in any open stage (code changed after
   the plan closed, e.g. rework in review) or `verified` in `dev`; a `task_update` / `task_patch`
   changing `progress` adds it only in `dev`, behind the same gate. The closing report and the
   closing progress never warn. A warning in `strict` mode too, never a
   refusal. The reminder and the session-start context say
   `Plan of <key> (<state>): the plan is closed (N/N done) …` only for a task in `dev`; other
   stages keep "all N steps done".
   `plan_step_add` on a `done` task is refused ("reopen it (task_set_stage) before adding steps");
   on `live` it is allowed and the answer says the step does not move the task.
5. **Draft hint.** A task in `dev` whose plan is a draft and whose newest plan revision is at least
   `plan_draft_hint_hours` old (default 1, `0` off): `portfolio_digest`, `project_digest` and
   `task_context` end with
   `Hint: <key>'s plan is a draft for N h: accept it (plan_accept) or ask for changes.` only when
   the caller is a coordinator session (named in `coordinators`, checked
   before the call links its hook session); `project_snapshot` has no caller identity and never
   carries it. Oldest first, at most five lines, then `…and N more drafts.`; the query finds only
   those tasks. The card's Plan badge shows `draft · N h`. Never a line for the agent.

Decided by default:

- Only a step added to an **accepted** plan is marked; on a draft plan (or a task without a plan,
  which then gets a draft) `plan_step_add` is a plain append with the reason in the history.
- A step renamed by `plan_set` loses its mark, like its checkpoint.
- The automatic move back to `dev` runs no stage rules (the plan's own write decides it) and does
  not mention the pulse.
- The rework and all-done lines are MCP answer notes, not rule warnings; the closed-plan warning
  is a note too, so the owner's board form never shows it.
- The all-done hint comes only from the update that closes the plan (a done step marked done again
  says nothing).
- N in the draft hint is whole hours, rounded down; the hint lines are appended at the end of the
  digest text, after its budget.
- "After the plan closed" is strictly later: a report in the same millisecond as the closing
  write does not count.

**Todo capture on Claude Code 2.1.318** (checked live with `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`):
`TaskCreate` and `TaskUpdate` (status with or without `activeForm`) are recorded and "Now doing"
shows under the current step; the parser needed no change. Known limit: the forwarder gives up
after 0.3 s per network step and stays silent, so a dropped hook loses that todo change until the
next one (seen once: a status-only `TaskUpdate` about 30 s after the create stayed pending until
the next update).

## Risks

| Risk | Mitigation |
|---|---|
| Todo tool payloads change between Claude Code builds | Defensive parsing, unknown shapes ignored, both generations supported |
| Reminders crowd the agent's context | Only on start, todo change and every N tools; never the same text twice; capped length |
| The plan turns into ceremony for small tasks | `S` tasks need no accepted plan; the todo counts as the agent's plan |

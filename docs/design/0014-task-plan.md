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

## Risks

| Risk | Mitigation |
|---|---|
| Todo tool payloads change between Claude Code builds | Defensive parsing, unknown shapes ignored, both generations supported |
| Reminders crowd the agent's context | Only on start, todo change and every N tools; never the same text twice; capped length |
| The plan turns into ceremony for small tasks | `S` tasks need no accepted plan; the todo counts as the agent's plan |

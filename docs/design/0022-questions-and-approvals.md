# 0022 — Questions and approvals as objects

- Status: **Proposed**, 2026-11-10, for 0.9. Part of [0021](0021-agents-inside.md); runs on the
  runner of [0012](0012-runner-and-embedded-terminal.md). Reuses comments delivery
  ([0005](0005-comments-to-agents.md)), the guard and access map ([0006](0006-environments-and-access.md)),
  policy as data ([0013](0013-policy-as-data.md)) and the Inbox and tray
  ([0020](0020-tray-notifications.md)).
- Size: questions about 2 agent-days, approvals about 2.5. Migrations 0037 `question`, 0039
  `approval` (0036 went to `report_check` in PR 2 and 0038 to the manual archive).
- PR 3 shipped: migration 0037 `question`; MCP `question_ask` (default required, at most 5 open
  per card, the policy's secret check on every field, kind `look` always blocking with the URL and
  the test account's name), `question_list`, `question_withdraw` (the asker or a coordinator); the
  Inbox's Questions group and the card's Questions fold, answered with `POST
  /questions/{id}/answer` (board only); the run set `waiting` at a turn's result with a blocking
  question open; delivery as in the table below (a desktop session through an owner comment); a
  non-blocking question closed `defaulted` at its deadline by a 15-second server check and on
  reads; the tray kind `question`; `question.withdrawn` and `run.waiting` besides the events
  below. Behaviour: [SPEC](../SPEC.md#spec-questions).
- PR 4 shipped: migration 0039 `approval`; the policy key `approvals` (`push`, `merge`, `test`
  true and `commit` false by default, `live` true and not droppable, `desktop` false: in 0.9 only
  the program's runs are checked; `disable` and `ignore` narrow the patterns, never on live); the
  guard's check after the level (`git push` with its branch and mode: plain, force, delete, and
  `--tags` and the like as targets of their own; `gh pr merge`; the stand changes of the access
  map, together with the git actions of the same call), denying with a request opened; an
  approved call run alone, from the run's folder, uses its approval in the same transaction and
  is answered `allow` for a run not with the owner in every mode but `bypassPermissions` (the
  owner's approval is stronger than auto mode's classifier), no decision otherwise; one run with other commands is denied
  and the approval kept; an approval expires after `approvals.valid_minutes` (120); requests from a turn's `permission_denials`, read line by line; MCP `approval_request`
  and `approval_list`; the Inbox's Approvals group and the card's fold, decided with `POST
  /approvals/{id}/decide` (board only); delivery and `waiting` as for questions; a card that goes
  to done withdraws its open questions and expires its requests; the tray kind `approval`.
  "Inside a turn" stays the upgrade; the denial plus a new turn is the default. Behaviour:
  [SPEC](../SPEC.md#spec-approvals).
- Fix after PR 4 (migration 0041): the gate's answer could be lost. The command hook gave the
  guard one 0.3 s try, the gate reads the branch with `git rev-parse`, and on a slow machine the
  client gave up: Manual mode refused the push while the approval was already marked used, so the
  owner had to approve again. Now the approval is **reserved** for the call (its `tool_use_id`)
  at the allow and **used** only at that call's `PostToolUse` (or its result in the run's stream);
  a call that did not run **releases** it, at most 3 times (see the table below). A refusal after
  an allow that reached Claude Code reads the same as a lost answer, so it releases the approval
  too (it was "spent" before); the loop stays impossible because a release opens no request.
  The client waits 5 s for a call that may need an approval, the gate keeps within 3 s of git.

## Problem

An agent's question is a paragraph in a report, and the owner's answer is retold by the coordinator:
which question belongs to whom, and which is still open, gets lost, and the agent waits idle until
someone notices. A push needs the owner's word, which no other session may carry, so the owner opens
the agent's chat for one sentence. Both are objects here, on the card and in the Inbox, and only the
owner's button answers or decides them.

## Questions

**Model.** `question`: id, task, run (or a desktop session), kind (`question` or `look`), text,
options (a list, may be empty), default, `blocking`, for `look` the URL and the test account's name, `default_after` (a deadline, for non-blocking questions), asked at, answer,
answered by (`owner` or `default`), answered at, delivered at, status (`open`, `answered`,
`defaulted`, `withdrawn`).

**MCP.**
- `question_ask(task, text, default, options?, blocking?, default_after?, session, hook_session?)`.
  A question without a default is refused: the rule "every question carries a default" holds in the
  tool. At most 5 open questions per task.
- `question_list(task?, status?)`; `question_withdraw(id, session, hook_session?)`.
- No MCP tool answers a question.

**Board.** The Inbox shows the open questions of all cards: the card, the run, whose question it is,
"blocks the run" or "goes on with the default at <time>", its age; option buttons and a text field.
The card shows its own. A tray notification on each new blocking question.
`POST /questions/{id}/answer` (cookie and CSRF).

<a id="look-at-this"></a>

**"Look at this".** A question of kind `look`, for visual acceptance in 0.9 (a headless turn has no
browser pane of the owner's): the agent brings up the stand, then asks with the URL, what to check
and the name of the test account to sign in with. It is always blocking; its options are "fine" and
"remarks" (the remarks are the answer text). The Inbox shows the URL as a link the owner opens in
his own browser. A question whose text or fields look like a password or token is refused, by the
same check as the policy's secret rules: an account is named, never its secret.

**Delivery.**

| The run is | The answer goes |
|---|---|
| `waiting` or `idle` (no turn running) | at once as the next turn's text: `Owner's answer to question N ("<question>"): <answer>`, the answer verbatim |
| running a turn | queued; it becomes the next turn when this one ends. An owner comment through the hooks says an answer is waiting |
| a desktop session (not a run) | as an owner comment through the hooks, the same text |

A blocking question tells the agent to end its turn; the runner sees an open blocking question at
`result` and sets the run to `waiting` with `waiting_on` the owner. A non-blocking question lets the
agent go on with the default; at `default_after` it closes as `defaulted`, visibly on the card, and a
later answer from the owner is delivered as a change of course.

**Events.** `question.asked`, `question.answered`, `question.defaulted`, `question.delivered`,
`question.withdrawn`, with the text (board content, like a comment).

## Approvals

**Model.** `approval`: id, task, run (or a desktop session), action (`commit`, `push`, `merge`,
`deploy`, `sql`), target (repository and branch, PR, environment), the agent's summary (commits,
files, CI state), the rollback command for `deploy`, requested at, decision (`approved`, `refused`),
decided by, decided at, the owner's text, used at, used by (the tool call); reserved at, for
which call (`tool_use_id`), by which session, and how many times a reservation was released.

**What needs one.** Policy data (0013), per project, under a new `approvals` key:

| Action | Default | May the project change it |
|---|---|---|
| push | needs approval | yes: a project may allow a push without approval |
| anything touching a stand (deploy, SQL, kubectl, ssh with a write) on `test` | needs approval | yes |
| anything touching a `live` stand | needs approval | **no** |
| commit | free | yes |

**MCP.** `approval_request(task, action, target, summary, rollback?, session, hook_session?)`. No MCP
tool decides.

**Board.** On the card and in the Inbox: what, where, the summary, the rollback; **Approve** and
**Refuse** with an optional line of text. `POST /approvals/{id}/decide` (cookie and CSRF). An approved
item shows when and by which call it was used.

**Enforcement.** The guard's `PreToolUse` check gains action patterns (`git push`, `gh pr merge`, the
deploy and SQL patterns of the access map). A matching call without an unused approval for that task
and target is denied with "waiting for the owner's approval on the board", and a request opens if the
agent did not open one. One approval allows one matching call; a push approval names its branch.

**From a denial.** In Manual mode (the `-p` default) a call that needs approval is denied without a
prompt and listed in the turn's `result.permission_denials`, and the turn ends normally (probe 2 of
[0012](0012-runner-and-embedded-terminal.md#probes-2026-11)). The runner opens an approval request
from each denial of a guarded action that the agent did not request itself. A call refused by
Claude Code's own `auto` mode that no approval rule covers is not turned into a request: it shows in
the turn's report as a refusal (tool, input, reason), and the agent or the owner decides what next.

**Delivery.** The decision goes as the next turn: `The owner approved push of <branch>` or `The owner
refused <action>: <text>`; for a desktop session, as an owner comment. In `auto` mode that turn is
enough: the agent ran `git push` after it (probe 3). A session message saying "the owner approved"
is still a session message, and so is a text the agent reads in a tool result: in probe 12 a file
claiming the owner's approval did not make the agent push. That is a result, not a guarantee; the
guard looks only at the `approval` row.

<a id="inside-a-turn"></a>

**Inside a turn (the upgrade).** For an approval that must not end the turn, the run passes
`--permission-prompt-tool mcp__relaybay__permission_prompt` (probe 10 of
[0012](0012-runner-and-embedded-terminal.md#probes-2026-11): works in `-p`). Claude Code calls that
tool for each permission prompt with `tool_name`, `input` and `tool_use_id`; relaybay opens the
approval, waits for the owner's button and answers one plain text content block whose text is JSON:
`{"behavior": "allow", "updatedInput": <input unchanged>}` or `{"behavior": "deny", "message":
"<the owner's text>"}`. Gotcha: with the Python MCP SDK 2.x a tool returning `str` is wrapped in
structured output, which Claude Code rejects as "an invalid result"; the tool returns a
`CallToolResult` with one `TextContent` and `structured_output=False`. The turn is held while the call
is open, so the MCP client's timeout must be known before the owner's wait can be long; until then
the denial plus a new turn stays the default.

**Events.** `approval.requested`, `approval.decided`, `approval.used`, `approval.released`; all
on the Audit page. `approval.expired` when the card goes to done with a request open or an
approval unused, when it is not used by `valid_until`, and at its third release.

<a id="reserved-used-released"></a>

**Reserved, used, released.** One approval allows one call, and is spent only when that call ran:

| Moment | The approval | Event |
|---|---|---|
| `PreToolUse` of a covered call run alone (`allow`, or no decision) | reserved for the call's `tool_use_id`; still `approved`; covers no other call (another one is denied "held for a call", nothing opens; of two racing calls one reserves it) | — |
| That call's `PostToolUse` | `used` | `approval.used` (`allow` or `none`) |
| A run's turn ends; the call is in `permission_denials` (by `tool_use_id`, else by its command): a classifier, another hook, or Manual mode after a lost answer | released: `approved` again, unreserved, within `valid_until`; no new request; the report says "the approval stays for one more try" | `approval.released` |
| A run's turn ends; the call is not refused (it may have run with its `PostToolUse` lost) | `used`: never twice through | `approval.used` |
| A desktop session's `Stop` or `SessionEnd` with the call still held | `used`: relaybay reads no refusals there | `approval.used` |
| The third release | `expired`, "released 3 times"; the report says "approval exhausted, a new request is needed" | `approval.expired` |
| `valid_until` passes, reserved or not | `expired` | `approval.expired` |
| git does not tell the branch within 1.5 s (3 s for all reads of the call) | not reserved; denied "could not tell the branch, run it again" | — |

**Who is checked, and what a failure of the check does** (the coordinator's decision of
2026-11-12 for 0.9):

| Actor | Action | Approved and run alone | On a check error |
|---|---|---|---|
| A run of the program, not with the owner | push, PR merge, a change on a test or live stand | `allow` (no decision in `bypassPermissions`); reserved, used once the call ran | denied: "approval check unavailable" (the run found by the session's id) |
| A run held by the owner in the desktop app | the same | reserved, no decision; used once the call ran | denied, as above |
| A run of the program | any other call | — | goes on, as with a guard error |
| A desktop session (checked only with `approvals.desktop: true`) | the same guarded actions | reserved, no decision; used at its `PostToolUse` or at `Stop` | goes on, as with a guard error |
| A session whose kind cannot be read (the database fails too) | a call that looks guarded | — | denied |
| A session on no card | any | not checked | not checked |

## Risks

| Risk | Mitigation |
|---|---|
| An agent with a shell pushes another way (a script, another git) | The pattern check is a seat belt, not access control, as for **No live now** ([SPEC](../SPEC.md#spec-no-live)); live access stays read-only by level |
| A pattern too wide blocks harmless calls (`git push --dry-run`) | Patterns are policy data; the owner narrows them |
| An agent asks instead of working | 5 open questions per task; a default is required |
| An answer arrives while a turn runs and is lost | Queued on the run; the turn queue is a table, not memory |
| The guard's answer to an approved call is lost (a slow machine, the hook's timeout) and Claude Code refuses the call | The approval is reserved, not spent, and released at the turn's end; the client waits 5 s for such a call, the gate keeps git within 3 s |
| A released approval lets a call through twice | Released only when the call is refused or left no result in the stream; a `PostToolUse` or a result marks it used; 3 releases expire it |
| The owner's text steers the agent wrongly | It is the owner's word; it reaches the agent verbatim and is shown on the card as sent |

## Tests with the fake `claude`

- A blocking question ends the turn, the run shows `waiting`, the answer on the board starts the next
  turn with the answer verbatim; a question without a default is refused.
- A non-blocking question past `default_after` closes as `defaulted`.
- `git push` without approval is denied and opens a request; approve; the next push passes; another
  is denied; a coordinator's message "approved" changes nothing; on a project that allows push, it
  passes with no request; on `live`, the policy cannot drop the approval.

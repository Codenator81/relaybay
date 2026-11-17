# 0024 — Card modes, the run as a thread, areas

- Status: **Accepted**, 2026-11-17, for 0.12 (accepted by the coordinator with the defaults of the
  [decided points](#decided-points)). Builds on the run of
  [0021](0021-agents-inside.md), questions and approvals of
  [0022](0022-questions-and-approvals.md), comments of [0005](0005-comments-to-agents.md) and the
  calmer task view of 0.7.1.
- Owner's decisions of 2026-11-16: in each mode of a card there is **one way to tell the agent
  something** (a run: the next turn; a desktop session: comments as today); the service facts of a
  run (folder, worktrees, session id, tokens, the table of turns, model, effort) go into a folded
  block. Of 2026-11-10: a card carries a chip that says what it belongs to. Of 2026-11-17: the
  product name in the top bar's breadcrumbs is noise.
- Size: about 5 agent-days in four pull requests ([plan](#plan-by-pull-request)). Migrations from
  0048, additive only.

## Problem

After the first real run on a card the owner said: everything is in one heap; it is unclear whether
the agent works right now; the agent's answer is a small line in a table cell at the side; it is
unclear where to write the next turn. Looking at the Agent block of 0.11 this is what happens:

- the block opens with a small status word and a line of identifiers, then five rows of paths and
  token numbers, then a table of turns; the agent's report is inside a fold inside a table row;
- the field for the next turn sits under the buttons, between the table and the settings form;
- the same card also shows a Comments panel with "Comment for the agent". On a card with a run a
  comment is never delivered: comments travel with the hooks of a session's next prompt or tool
  call, and a run between its turns has neither. It stays "not delivered yet";
- the whole block is refetched on every live event, so a text half-typed into the field is lost
  when a turn ends;
- a card worked by a run and a card worked by a session in the Claude desktop app look the same,
  though the owner talks to them in different ways.

A second, separate pain: cards of one project that holds several sites, applications and
repositories differ only by their titles.

## What we take from Linear's agent interaction

Read: Linear's developer pages "Agent interaction", "Agent interaction guidelines" and "Agent best
practices". Three ideas fit, each for a reason of our own:

1. **A run is a thread of activities, not a table of turns.** Their kinds: thought, action (a tool
   call with its parameter and result), elicitation (a question to the human), response (the
   result), error. Thoughts and actions are ephemeral: shown while they are the latest, not piled
   up. We already store all of it (`run_turn`, `run_event`, `question`, `approval`, `report`);
   only the view changes.
2. **The state is derived from the latest activity**, never set by the agent: pending, active,
   awaiting input, complete, error, stale. We derive ours from the run, its last turn and its open
   questions the same way.
3. **The human's reply is a prompt in the same thread**: a follow-up and an answer to a question
   go through one channel, and the agent cannot write a prompt. This is the owner's "one way to
   tell the agent" rule.

Not taken: their advice that an agent posts its own activities over an API. Ours come from the
stream of the owner's own `claude`; an agent writes nothing extra to be seen.

## Card modes

A card has a **mode**, derived on every read, never stored and never set by hand:

| Mode | When | The one channel to the agent |
|---|---|---|
| **agent** | the card has an open run (`kind = card`, not ended); or its latest run ended and no session is bound after it | the next turn |
| **session** | a desktop or terminal session is bound and no run is open | comments, as today |
| **empty** | neither | a comment that waits for whoever comes first |

An open run wins over a bound session. Ending the run and binding a session turns the card into a
session card; starting a run on a session card turns it into an agent card. Nothing is migrated:
the mode is a function of rows that exist today.

Both modes share one skeleton, so the eye lands on the same places:

```
+--------------------------------------------------------------+
| acme-shop-12  [web shop]            Overview | Plan | Review |
| Checkout: total ignores the coupon                           |
+--------------------------------------------------------------+
| STATE, large        one line of why        [the state's act] |
+--------------------------------------------------------------+
| the main content of the mode, full width                     |
|   agent:   the thread                                        |
|   session: the last report, then the comment thread          |
|                                                              |
+--------------------------------------------------------------+
| > Run details / Session    (folded)                          |
| > Task: description, done so far, decisions, ...  (folded)   |
+--------------------------------------------------------------+
| [ one text field                                   ] [Send]  |  <- stays at the bottom
+--------------------------------------------------------------+
```

The text field is outside every refetched fragment: a live event never clears what is typed.
Ctrl+Enter sends. The stage, "waiting on", "next step", branch and code row stay a compact line
under the title in both modes.

### The owner's word in agent mode

One function takes everything the owner says to the agent of a card and picks the road by the
card's mode. In agent mode every road ends in a turn of the run:

| What the owner does | Session mode (as today) | Agent mode |
|---|---|---|
| writes in the text field | an owner comment, delivered by hooks | the run's next turn (starts now; waits when a turn runs or the run is with the owner in the terminal or the desktop app) |
| answers a question | owner comment | a turn "the owner's answer to question N" (0.9, unchanged) |
| approves or refuses | owner comment | a turn (0.9, unchanged) |
| rejects or accepts a review step with a note | owner comment | a turn "the owner's decision on review step N" |
| saves a screenshot annotation | owner comment | a turn with the annotation's text and the picture's path |

- The Comments panel and its form are not shown on an agent card. Comments that exist on the card
  (from its time as a session card, and the agent's own `comment_add` notes) stay readable inside
  the thread, each at its time.
- A comment row is still written for a review decision and an annotation (the Review tab shows
  it); it gets the turn it went with (`comment.turn_id`) and reads "sent as turn N" in place of
  "not delivered yet". The same wording questions and approvals use since 0.9.
- **Owner comments stuck undelivered on an agent card** (written before 0.12, or written on an
  empty card before a run started) go at the head of the run's next turn, each marked "owner
  comment #N", and are then delivered. The upgrade itself starts no turn: no token is spent
  without the owner's action.
- A text sent while a turn runs waits for that turn's end and shows in the thread as "waits for
  the running turn" with a Cancel button. It is not pushed into the running turn.
- Permission is carried only by the owner's button. A text typed into the field never approves an
  open approval, whatever it says; the approval's own buttons do.
- The coordinator's `run_start` and its messages to a run are unchanged; in the thread they show
  with their author, apart from the owner's.

### The session card

What changes on a card worked by a desktop or terminal session is order, not behaviour:

- the state line shows the session's own state, large: **Working** (with its last activity),
  **Idle**, **Waits for you** (an open question or approval, or "waiting on" names the owner),
  **Ended**, **No session**;
- the last report is the main content, the comment thread follows it, and the comment form is the
  text field at the bottom ("Comment for the session: it arrives with its next prompt or tool
  call");
- the Agent block shrinks to one row, "Start an agent on this card", that unfolds the start form;
- the session's binding, id and history go into the folded Session block.

Comments, their delivery, acknowledgement and the wake-up hook work as in 0.11.

## The agent card

### State

Seven states, one function (`domain/runs.py`), the same answer on the card, on the board and over
MCP. Checked top down, the first that holds wins:

| State (shown) | Holds when | Line of why | Action in the line |
|---|---|---|---|
| **Ended** | the run ended | who ended it and when | Start a new run |
| **With you** | the run is with the owner in the terminal or the desktop app | where and since when; "texts wait until Back to the program" | Back to the program |
| **Needs your answer** | an open blocking question or an open approval of the run | the question's first line | jumps to it in the thread |
| **Working** | a turn runs | turn N, time since its start, the latest action; **quiet for N min** in place of the action when no line came for 120 s | Stop |
| **Queued** | a turn waits for a slot, memory, a brake or a budget | the stored reason | Start anyway (not past a brake) |
| **Failed** | the last turn ended `error`, `lost` or `failed_to_start` | the reason, as stored | none; the text field retries |
| **Stopped** | the last turn was stopped | by whom | none |
| **Your move** | the last turn ended `success` | "reported N min ago" and the report's first line | none |

"Starting" is Working before the stream's first line. "Quiet" is a mark, not a state of its own: a
long tool call is quiet too, and only the owner can tell a hung turn from a slow test run.

### Thread

Built per request from rows that exist; no activity table. The latest run of the card is open,
earlier runs are one folded line each ("Run 12 · ended · 3 turns · 2 days ago").

| Activity | From | Shown |
|---|---|---|
| prompt | `run_turn.prompt`, `sent_by` | the owner's text as written; the first turn's brief folded to "Brief" with its first lines; a turn made by relaybay (an answer, a decision) named for what it carries |
| thought | assistant text lines of `run_event` | while its turn runs: the latest one; after: inside the turn's fold |
| action | tool calls of `run_event`, subagent lines nested | while its turn runs: the latest one with its tool and argument; after: inside the fold |
| question, approval | `question`, `approval` of the run | the item with its own buttons, at the time it was asked; answered ones as one line |
| response | the turn's `report` (or `result_md`) | full markdown, full width, with its state and size badges and `verified` marks; the latest open, earlier ones folded to their first line |
| error | a turn's outcome other than `success`, refusals | the outcome and reason; denied tools by name |
| note | run events (started, settings changed, handed to the terminal, worktree kept) and comments | one muted line |

A finished turn is: its prompt, one fold "N steps · M tokens · duration" (loads its thoughts and
actions when opened, the last `board_lines` as today), then its response. So a card after three
turns shows three prompts and three responses and nothing between them.

With the project's event retention off the thread still has prompts, questions and responses;
the fold says the lines were not kept.

### Live

A running turn stores its lines in batches and sends no board event for them (0.9). 0.12 adds a
signal for the pages that have this card open: at most one per run in two seconds, not stored in
the event log, carrying only the run's id. On it the page refetches one small fragment, the
running turn's tail (latest thought, latest action, counters), and the state line. The thread
above it and the text field are not touched. A turn's end refetches the thread once.

### Run details

One folded block under the thread, closed by default, remembering its state per browser like the
other folds: the folder, worktrees, where hooks and MCP come from, the Claude session id and the
desktop link, tokens by kind, the table of turns as in 0.11, the "from the next turn on" form
(model, effort, permission mode), Terminal, Open in the desktop app, End run. Nothing is removed,
only moved.

The Questions, Approvals and Last report panels of the Overview are not repeated on an agent card:
their content is in the thread. The Task fold keeps description, done so far, decisions, earlier
reports, messages and the edit form.

### On the board

An agent card in a column shows its run's state in place of the desktop session icon: a dot and a
word (working, needs answer, your move, queued, failed, with you). A session card keeps the icon
and the session's dot as today.

## Areas

The chip is an **area**: what part of the project a card belongs to ("web shop", "mobile app",
"api").

- **One area per card, or none.** It answers one question, "what is this about", and one coloured
  chip reads at a glance; several labels per card become a tag cloud. More than one is not planned.
- **A list per project that grows from use.** The card's form has a field with the project's areas
  as suggestions; a name not in the list is added to it. Names match without regard to case and
  outer spaces, so "Web shop" and "web shop" are one area. Up to 24 areas a project, 32 characters
  a name.
- Why not free text on each card: no colour that stays, no filter, and two spellings of one thing.
  Why not the project's repositories: an area is often not a repository (two sites in one
  repository, one application across three), and the Code row already shows repositories.
- **Colour** comes from a palette of eight by the area's place in the list and can be changed.
  Rename, recolour, reorder and delete are on the project's settings page; deleting an area clears
  it from its cards (the event log keeps what was).
- **Where it shows:** on the board card's top line after the key; in the task dialog's head and on
  the task page; in Inbox and review queue rows, the quick switcher and All boards; a board filter
  "Any area" next to the stage filter, kept in saved filters.
- **MCP** (additive, contract v1): `task_create(area=…)`, `task_update(area=…)`, `area` in
  `task_patch` and in `relaybay://schema/task`; `task_list(area=…)`; one line in `task_get`,
  `task_context` and the digests; `project_snapshot` lists the project's areas. An unknown name
  from an agent is added like from the form, and the response says so and lists the areas that
  exist, so an agent sees a near-duplicate it just made. "-" clears.
- **The backlog already has an area** (free text with suggestions, since 0.5). It is the same
  notion and keeps its name: the backlog's field now suggests the project's areas first, and "To
  plan" gives the new card the item's area (added to the project's list when new; a text over 32
  characters is not carried). Backlog rows are not migrated and stay free text.
- Data: `project_area (id, project_id, name, color, position, created_at)`, unique on the project
  and the lowered name; `task.area_id` nullable, `ON DELETE SET NULL`. Both by `ALTER TABLE` and
  `CREATE TABLE`; no table is rebuilt. Markdown export and import carry the area by name.

## The top bar

On pages that have the sidebar the product name in the breadcrumbs becomes a small home icon (an
inline SVG, the same link to the start page, labelled "Home" for screen readers): the window's
title and the browser tab already say the name, and "relaybay / relaybay / Backlog" on the
project of the same name reads as a mistake. Pages without the sidebar (sign-in) keep the word.

## MCP contract

Nothing a bound desktop session uses changes. Additions only:

- `area` as above;
- `task_get` and `task_context`: a line `mode: agent | session | empty`;
- `run_status`: a line `state: …` with the shown state and its reason;
- `comment_add` by the agent of a run keeps working and shows in the thread as a note. An owner
  comment can no longer be born on an agent card from the board; `comments_list` still returns the
  card's comments.

## Risks

- **A turn is dearer than a comment.** In session mode a comment rides a prompt that happens
  anyway; in agent mode every text is a turn that reads the whole context from cache (probe 19 of
  0023). The field says "starts a turn" on its button's title, and stuck comments never start one
  by themselves.
- **A review of ten steps makes ten turns.** Decisions made while a turn runs queue as separate
  texts; when the turn ends, all texts waiting from relaybay's own roads (review decisions,
  annotations) go as **one** turn. The owner's typed texts stay one turn each.
- **The thread of a long run is heavy to build.** Only the latest run is expanded, only its
  responses are rendered, lines load per fold; the live tail is one small query. A card with 40
  turns must render under the page budget the board tests already hold.
- **Mode flips surprise.** Binding a session to a card with an ended run changes its look; the
  state line always names what the card is waiting on, and the thread of the ended run stays under
  "Earlier runs" in the Task fold of a session card.
- **Area sprawl from agents.** Bounded by the limit of 24, the response that lists existing areas,
  and the settings page where two areas are merged by renaming one into the other.

## Tests with the fake `claude`

- The state function over every combination of run status, last outcome, open question and
  hand-over; one table-driven test.
- A turn from each road in agent mode (field, review decision, annotation) and the same actions on
  a session card still writing comments delivered by hooks.
- A comment stuck before the upgrade goes with the next turn once, marked, and reads "sent as turn
  N"; the upgrade starts no turn.
- The live signal: throttled, not stored, refetches the tail only; a text typed in the field
  survives a turn's end (browser test).
- Text that says "approved" leaves an open approval open.
- Areas: case-insensitive match, the limit, delete clears cards, filter, MCP create and patch, the
  hygiene and contract drift checks.
- Migration from a 0.11 database with runs, questions and comments: every row kept.

## Decided points

Defaults taken where the materials did not say; each can be turned back without a migration,
except D1's table.

| # | Point | Default | Other way |
|---|---|---|---|
| D1 | How the chip is set | one area per card from a project list that grows from use | free text per card; several labels |
| D2 | Its name on the board | "Area" | "Label", "Component" |
| D3 | A text sent while a turn runs | waits for the turn's end | pushed into the running turn through its hooks |
| D4 | Owner comments stuck on an agent card | go with the next turn, start none | each starts a turn at once; or a "Send as a turn" button |
| D5 | Quiet mark | after 120 s without a line | Linear's 10 s (too short for a test run); none |
| D6 | Review decisions and annotations made in a row | one joined turn | a turn each |
| D7 | The mode | derived, an open run wins | a switch on the card |
| D8 | The breadcrumb fix (a home icon for the product name) | in 0.12, PR 1 | a patch release before it |
| D9 | Backlog areas | same notion, carried by "To plan", backlog rows stay free text | backlog rows moved to `project_area` ids |

## Plan by pull request

| PR | What | Review | Size |
|---|---|---|---|
| 0 | This design, ROADMAP, the designs index | coordinator | 0.2 |
| 1 | Areas: migration 0048, project settings, form, board chip and filter, lists, MCP, export and import; the top bar without the product name | reviewer: migration, MCP contract | 1 |
| 2 | Modes and one channel: derived mode and run state in the domain and over MCP; the owner's word routed by mode (field, review decision, annotation; `comment.turn_id`, migration 0049; stuck comments; joined turns); the shared skeleton and the session card | reviewer: delivery, migration | 1.5 |
| 3 | The agent card: state line, thread, text field outside the refetch, live tail, Run details fold, the run's state on the board card | reviewer: live signal, page weight | 2 |
| 4 | Release 0.12.0 | — | 0.3 |

A start on a version the data folder has not run yet backs the database and the settings files up
into `<data>/backups/` before the migrations run ([0016](0016-desktop-program.md)); 0.12 relies on
it and adds nothing to it.

## Acceptance (0.12)

On the installed program: a card with a run, seen while a turn works and after it reported; the
next turn sent from the field; a question answered from the same field; a session card with a
comment delivered as before; two cards of one project told apart by their areas and filtered by
one.

# relaybay specification

How relaybay behaves **now**: the concepts and every shipped feature. This page never describes
plans; upcoming work lives in the [roadmap](ROADMAP.md) and the [design documents](design/index.md).
When a feature ships, its final behaviour moves here in the same pull request.

Version covered: `0.2.0`. Anchors of the form `#spec-…` are stable and may be
linked from code, issues and other documents.

Contents:
[1. Concepts](#spec-concepts) ·
[2. Board](#spec-board) ·
[3. MCP contract](#spec-mcp) ·
[4. Hooks](#spec-hooks) ·
[5. Setup and integrations](#spec-setup) ·
[6. Working rules](#spec-rules) ·
[7. Environments and access](#spec-environments) ·
[8. Memory and context](#spec-memory) ·
[9. Security model](#spec-security) ·
[10. Configuration](#spec-config) ·
[11. Data model and migrations](#spec-data)

<a id="spec-concepts"></a>

## 1. Concepts

<a id="spec-project"></a>

**Project.** A unit of work, usually one repository: `slug` (lowercase letters, digits, dashes),
`name`, optional `repo_path` and `description`. Created or updated with `project_upsert`.

<a id="spec-task"></a>

**Task.** Belongs to a project and has a key `<slug>-<n>` (`acme-shop-12`), numbered per project.
Fields: title, stage, stage note, waiting on, next step, branch, document path, description,
progress, priority, size (`S` / `M` / `L`, [below](#spec-plan-size)), the external tracker fields
and a `version` that grows with every write. The
editable fields come from one list (`domain/task_fields.py`) that drives write validation, the
`relaybay://schema/task` resource and the "Editable fields" line of `task_get`.

<a id="spec-stages"></a>

**Stage.** A fixed set: `plan → dev → review → test → live → done`. `review` is where the owner
accepts the work step by step ([below](#spec-review)). Any transition is allowed (work jumps back
from `test` to `dev`); the event log keeps the history. Moving to `done` sets the closed time.
Display labels are configurable ([§10](#spec-config)); the keys stay English.

<a id="spec-review"></a>

**Review checklist.** A task can carry an ordered checklist of what the owner should check by eye
(the Owner check, [design 0007](design/0007-review.md)): each step has a title, an optional "what,
how and why" text (markdown) and a status `open`, `accepted` or `rejected` with who decided and
when. Agents write it with `review_steps_set` (it replaces the list: step N keeps its status and
screenshots while its title and details stay the same, a changed step is `open` again, steps past
the new end are removed and their screenshots stay on the task) and change one step with
`review_step_update` (text, or `status: open` to ask for a new look after a fix). Only the owner
accepts or rejects, on the board. Each decision is an owner comment linked to the step
(`comment.step_id`) and reaches the bound session through the comment path ([§4](#spec-hooks)):
"Step 2 'Login with 2FA' accepted — continue with step 3", "… accepted — all 3 steps accepted",
"Step 2 '…' rejected: <comment>", "… reopened", or "On step 2 '…': <comment>". A decision
the step already has, without a comment (a double click, a stale panel), writes and delivers
nothing. Moving a task to
`review` or `test` while its checklist is empty adds a hint line to the MCP answer (not a rule
warning). Events: `review.step_added`, `review.step_updated`, `review.step_removed`,
`review.step_accepted`, `review.step_rejected`.

<a id="spec-plan"></a>

**Plan and checkpoints.** A task can carry an ordered plan ([design 0014](design/0014-task-plan.md)):
each step has a title, an optional "what and why" (markdown, up to 2000 characters) and a status
`pending`, `in_progress`, `done` or `blocked`. Agents write it with `plan_set` (it replaces the
list: step N keeps its status and checkpoint while its title stays the same, a renamed step is
`pending` again, steps past the new end are removed, `[]` removes the plan; a write that changes
nothing is not kept) and move one step with `plan_step_update`. A **done** step is a checkpoint:
`done_at`, an optional one-line outcome, an optional link (a report of the same task, given as
`12`, `#12` or `report:12` and stored as `report:12`, or an http(s) URL such as a pull request) and
a required **conformance** line, `as planned` or `deviation: <why>`. Leaving `done` clears the
checkpoint; marking a done step done again keeps its `done_at`. An outcome may also say why a step
is `blocked`.

<a id="spec-plan-status"></a>

**Plan status** lives on the task: none while there are no steps, `draft` once written,
`accepted` (with who and when) after the owner accepts it on the board or the coordinator calls
`plan_accept` (refused for the session bound to the task). A change of the step titles or their order turns an accepted plan back to `draft`
(the `plan_set` answer says so); step statuses and "what and why" edits do not. Every plan write
(set, step update, acceptance) adds a `plan_revision` row with the whole plan as JSON, its author
and a reason. Events: `plan.set` (steps, status, previous status, whether the titles changed,
reason), `plan.step_updated` (position, title, status, previous, conformance, outcome),
`plan.accepted`.

<a id="spec-plan-size"></a>

**Task size.** `size` is an optional field (`S`, `M`, `L`; `task_update`, `task_patch`, the board
form; `-` clears it). A report whose first line names a size sets it when the task has none (a
`task.updated` in the same transaction); a size already set is kept. `S` tasks may go without an
accepted plan ([§6](#spec-plan-rule)).

<a id="spec-plan-todo"></a>

**The agent's todo (second layer).** The Claude Code todo list of the session bound to a task is
read from its hooks ([§4](#spec-plan-hooks)) and shown under the current plan step; it is never
merged into the plan. A task with no plan steps shows the todo as "Agent's plan, not accepted".

<a id="spec-attachments"></a>

**Screenshots (attachments).** Images of a task, optionally of one step, with a markdown caption.
`attach_file` copies a file (`path`) or decoded `content_base64`; the owner can paste one into a
step on the board. A path that starts with two slashes or backslashes (a UNC share, `\\?\` and
`\\.\` device paths, named pipes) is refused by its text before the disk is touched, unless it lies
under a share listed in `attachment_roots`. Only PNG, JPEG, WebP and GIF are kept, recognised by their magic bytes (never
by name or claimed type; SVG and everything else is refused); at most `attachments_max_mb` (10)
per file and `attachments_task_max_mb` (200) per task. A `path` must be absolute; symlinks and
`..` are resolved before the check, and the real path must be a regular file (no device, pipe or
folder) inside an allowed folder: the project's `repo_path`, the user's temp folder or one of
`attachment_roots`, and not in relaybay's data folder itself or its `attachments/` folder. The
opened file must be the one that was checked (its stat from the check and `fstat` of the open file
are the same file). The same image (SHA-256) on the same task is stored once; attaching it again,
to any step, reuses it and the answer names the step it is on. Files live at `<data dir>/attachments/<project>/<task key>/<random hex>.<ext>`; the database
keeps only the stored name, the original name (display only), type, size, hash, caption, author
and time. Removing an attachment deletes its file after the commit; on every start a sweep deletes
files and folders under `attachments/` that no row names (left by a crash, a failed delete, a
removed task). Events: `attachment.added`, `attachment.updated` (caption), `attachment.removed`.

<a id="spec-session"></a>

**Session and binding.** A session is whoever works on a task: a Claude desktop app session
(`local_<uuid>`, which gets an "Open in desktop" link from `desktop_link_template`), a plain
human-readable name, or a session the hooks created (`kind = 'hook'`). A task is bound to at most one
session at a time; every binding is kept in the task's binding history. A plain name passed to
`task_bind_session` for a task bound to a desktop session does not rebind: it becomes that session's
*alias* and from then on resolves to it. Aliases are global; none is created when a plain session of
that name exists (the call warns and changes nothing). The session identity is for display only; the
token is the only authorisation.

<a id="spec-report"></a>

**Report.** Markdown appended to a task, usually at the end of a stage. Its first non-empty line is
the summary (up to 200 characters). A first line `state · size · gist` with a known state
(`inspected`, `changed`, `verified`, `committed`, `pushed`, `blocked`; a second word such as
"verified locally" is allowed) and size (`S`, `M`, `L`) also sets the report's state and size, shown
as badges.

<a id="spec-progress"></a>

**Progress, next step, waiting on.** `description` says what to do and why; `progress` is the running
summary of everything done so far (rewritten, not appended). `next_step` is what happens next,
`waiting_on` who acts next. `task_set_stage` and `report_add` take `next_step` and `waiting_on`
(and `report_add` takes `progress`) and save them in the same transaction. A next step written
before the task's last stage change is **stale**: shown muted with a "stale" marker on cards and in
the task dialog, `(stale)` in MCP output. Writing it again, even with the same text, confirms it
when it is stale or when the same write changes the stage; otherwise an unchanged next step is a
no-op. The board form confirms it only when its text was edited.

<a id="spec-tracker"></a>

**External tracker mark.** `tracker_ref` (issue key, up to 100 characters), `tracker_name` (free
text), `tracker_url` (`http://` or `https://` only) and `tracker_logged` ("the work log is entered
there"). Every issue key a task has had is kept in its tracker history; a different `tracker_ref`
appends an entry, logging and unlogging stamp the current entry. **Automatic unlog:** new work on a
logged task (a report, a changed `progress`, or a move back out of `live` / `done` to an earlier
stage) clears the mark in the same transaction with a `task.tracker_unlogged` event carrying the
reason. Metadata edits keep the mark; an explicit `tracker_logged` in the same write wins; a new
`tracker_ref` starts as not logged unless `tracker_logged` is passed with it.

<a id="spec-decision"></a>

**Decision.** A short text recorded for a project (scope `project`) or one of its tasks (scope
`task`) with `decided_by`, a structured source and two time axes; shown in the project snapshot
and in `task_get` (current ones only), listed and searched with `decision_list`. A newer decision
can supersede an older one, which stays in history ([§8](#spec-memory-decisions)).

<a id="spec-rules-concept"></a>

**Working rules.** How agents are expected to work with tasks: the report format and what a stage
move needs. Served to every client and checked on stage moves ([§6](#spec-rules)).

<a id="spec-coordinator"></a>

**Coordinator.** A session that oversees many projects (creates tasks, binds sessions). It is a role,
not a feature: it uses the same tools as any session.

<a id="spec-events"></a>

**Event.** Every write appends a row to the event log in the same transaction as the state change.
The event id is monotonic and doubles as the Server-Sent Events id. Kinds: `project.created`,
`project.updated`, `task.created`, `task.updated`, `task.stage_changed`, `task.session_bound`,
`task.session_unbound`, `task.tracker_unlogged`, `report.added`, `decision.added`, `session.seen`,
`session.status_changed`, `session.alias_added`, `comment.added`, `comment.delivered`,
`comment.read`, `environment.created`, `environment.updated`, `environment.deleted`,
`environment.access_added`, `environment.access_updated`, `environment.access_removed`,
`environment.proposed`, `environment.proposal_accepted`, `environment.proposal_rejected`,
`live.action`, `document.updated`, `memory.added`, `memory.updated`, `block.updated`
(`decision.added` carries `supersedes` when it replaces a decision). A stage change that misses a rule carries the warnings in its payload.

<a id="spec-comments"></a>

**Comment.** A message on a task's thread: from the owner (written on the board, for the agent of
the bound session) or an agent's reply (`comment_add`, author = the calling session). Markdown, up
to 4000 characters. An owner comment is *sent* when written, *delivered* when a hook of the bound,
exactly linked session handed it to the agent ([§4](#spec-hooks-comments)), *read* when the agent
calls `comment_ack` or replies. An agent reply is unread until the owner marks it read or writes a
comment on the task.

<a id="spec-environments-concept"></a>

**Environment.** A project's local, test or live stand with an access map that holds references
only and the agents' permission level there ([§7](#spec-environments)).

Not shipped yet, therefore not concepts of this release: the review checklist, memory notes and
documents (see the [roadmap](ROADMAP.md)).

<a id="spec-board"></a>

## 2. Board

Server-rendered HTML (Jinja2 + htmx, [ADR-0001](adr/0001-htmx-sse.md)), light and dark theme from
the system setting, usable on a phone. A header bar holds the navigation button, the brand link,
breadcrumbs and a live-status indicator (`connecting` / `live` / `offline`).

<a id="spec-board-nav"></a>

**Navigation.** Every signed-in page has a left sidebar ([design 0004](design/0004-navigation.md)):
**Inbox** with a live count badge, **All boards**, **Activity**, **Settings** (the Integrations
page), then **Projects**, each expandable (the current one is open) with **Board** and
**Sessions**, **Stands & access** and **Docs & rules**. The current item is highlighted (`aria-current="page"`). The header button hides or
shows the sidebar on wide screens; the choice is remembered in the browser (`localStorage`, the
sidebar is shown when storage is unavailable). Below 760 px the sidebar is a drawer opened by the
same button and closed by Esc, a click outside or following a link. Items whose feature does not
exist yet are not shown.

<a id="spec-board-pages"></a>

| Page | URL | Content |
|---|---|---|
| Projects | `/` | Projects with task counts per stage. |
| Project board | `/p/<slug>` | One column per stage on two pages (`?page=2`, [below](#spec-board-paging)); on narrow screens a stage tab bar shows one column at a time. |
| Task dialog | `/p/<slug>?task=<key>` | The task over the live board (full screen on a phone), tabs Overview and Review (`&tab=review`); closes with Back, Esc or a click outside. |
| Task page | `/t/<key>` | The same details as the dialog on a page of its own. |
| Inbox | `/inbox` | Everything that needs the owner, across projects ([below](#spec-board-inbox)). |
| Sessions | `/p/<slug>/sessions`, `/sessions` | The sessions of a project, or all sessions ([below](#spec-board-sessions)). |
| Activity | `/activity` | The event feed and the actions on live ([below](#spec-board-activity)). |
| Stands & access | `/p/<slug>/environments` | Environments, access map, permission levels and agent proposals ([§7](#spec-environments-page)). |
| Docs & rules | `/p/<slug>/docs`, `/p/<slug>/docs/<doc>`, `/p/<slug>/docs/<doc>/diff?from=&to=` | Documents, decisions, memory notes, blocks, the effective rules and search ([§8](#spec-memory-page)). |
| Integrations | `/settings/integrations` | Claude Code on this machine ([§5](#spec-setup)); `/settings` redirects here. |
| Notifications | `/settings/notifications` | Browser notifications: state and the button that asks for the permission ([below](#spec-board-notify)). |
| Screenshot | `/files/<id>` | One stored image behind the board cookie ([§9](#spec-security)). |
| Policy | `/settings/policy` | The effective policy per scope with the source of each value, preset, layer editors and agent proposals ([§7](#spec-policy-page)). |
| Sign-in | `/login` | Token form; also accepts `?token=…` once and sets the cookie. |

<a id="spec-board-paging"></a>

**Paged board.** Six columns do not fit one screen: on wide screens page 1 shows plan, dev,
review and test, page 2 live and done. The pager next to the search (links "Plan · Dev · Review ·
Test →" and "← Live · Done") and the keyboard ← / → switch pages; the keys are ignored inside
inputs, text areas, selects and editable text, with a modifier key, and while a dialog or the
screenshot viewer is open. The page is in the URL (`?page=2`, kept with search and archive
filters; out of range clamps), switching does not reload, and live updates keep refreshing every
column. Below 760 px there is no pager: the stage tab bar has six tabs.

<a id="spec-board-cards"></a>

**Cards** show only the essentials: key, tracker badge (`<tracker_name> <tracker_ref>`, a link when
a URL is set, ✓ when logged, "not logged" with `require_tracker_log`) and an icon-only "open in
desktop" link with the session's hook status dot in the top row; then title, waiting on, branch and
the next step (two lines, muted with "stale" when stale). Done cards hide waiting on and the next
step. A task with a checklist shows **Review 2/5** (accepted / all; green when all are accepted,
red-tinted while one is rejected), which opens the dialog on its Review tab. A task with a plan
shows **Plan 2/5** (done / all) with `· draft` while it is not accepted, the number of deviations
highlighted and `· now: <step in progress>` (one line, cut with an ellipsis), which opens the
dialog on its Plan tab.

<a id="spec-board-dialog"></a>

**Task dialog / page** shows every field (stage with note, waiting on, next step, branch, tracker,
document path, updated time and version), description and progress in full, the latest reports
with state and size badges, decisions, the bound session with its hook status and last activity,
"Open in desktop" and "Copy link", the session binding history, the tracker history, an
**Unbind session** button (with confirmation), a session picker (**Bind**: the project's sessions
first, then the others, most recently seen first; the same binding history and
`task.session_bound` event as `task_bind_session`, actor `board`), the comments and the edit form. A stage move that
misses a rule shows the warning in the form (or the refusal in `strict` mode).

<a id="spec-plan-board"></a>

**Plan tab** (task dialog, `?tab=plan`; a Plan section on the task page; tabs Overview | Plan |
Review): the plan state (**Accepted** with who and when, or **Draft** with an **Accept plan**
button: board-only, cookie and CSRF, refused with a Bearer token; actor `owner`), "2 of 5 done ·
1 deviation", then the steps with their status, "what and why" and checkpoint (conformance,
outcome, link, time); a deviation is highlighted. Under the current step (in progress, else the
first one not done): "Now doing: <activeForm of the todo item in progress>" and the agent's todo
collapsed. Without plan steps: the todo marked **Agent's plan, not accepted**, or a note on how
the agent writes a plan. **History (n)** lists every revision, newest first, with its reason,
author and time and the titles it changed (`- 2. old` / `+ 2. new`). The tab refreshes live on
`plan.*` and `session.todo_updated` events of its task; the card on `plan.*`.

<a id="spec-board-review"></a>

**Review tab** (task dialog; a Review section on the task page): "2 of 5 accepted · 1 rejected",
then the steps in order. Each step has an Accept checkbox (checking accepts, unchecking reopens),
its title and status, an expandable **What, how and why**, its screenshots as thumbnails with
captions (each with **Remove**, confirmed) and a comment box with **Comment**, **Reject** and
**Accept** (the comment travels with the decision). Screenshots of no step follow under "Other
screenshots". Pressing Ctrl+V with an image in the clipboard while the focus is inside a step
uploads it to that step (`POST /p/<slug>/t/<key>/review/<step id>/paste`, the raw image as body,
the CSRF token in `X-Relaybay-CSRF`, signed-in browser only, the same checks as `attach_file`;
413 above the size limit). All Review writes are board-only (cookie and CSRF, refused with a
Bearer token). The tab refreshes live on `review.*` and `attachment.*` events of its task, but not
while one of its comment boxes holds text.

**Viewer.** A thumbnail opens a full-screen viewer over the dialog: the image fitted to the
screen, its caption below, a counter "3 / 7" across the task's screenshots, ‹ › buttons and ← / →,
Esc or Close to leave (the dialog stays open), swipe left or right on phones.

<a id="spec-board-notify"></a>

**Browser notification.** With the permission granted, every open board tab shows "Waiting for
your acceptance: <task title>" when a `task.stage_changed` event moves a task to `review` or
`test` (one per task: the task key is the notification tag); a click opens its Review tab. The
permission is requested only by the **Allow notifications** button in Settings → Notifications,
never on page load; the page shows whether this browser allows, blocks or has not decided.

<a id="spec-board-comments"></a>

**Comments** (task dialog and task page): the thread oldest first, then a comment box (markdown, up
to 4000 characters, CSRF-protected form). Each owner comment shows `sent <age>`, then
`delivered <age> to <session>` or `not delivered yet`, and `read <age>` once acknowledged (exact UTC
times on hover); agent replies show their author, age and `unread` until marked read. **Mark replies
read** marks the task's unread replies read; sending a comment does the same. A note under the
thread says which session a new comment goes to, or that none is bound and it waits. The thread
refreshes live on `comment.*` events of its task; a draft in the box is kept (on the task page, other
changes then refresh the details instead of reloading the page).

<a id="spec-board-done"></a>

**Done column and archive.** The done column shows tasks closed within `done_window_days` (default
7), capped at 200 with a note. Older done tasks are archived on the board only: "Show archived"
(`?archived=1`) and the search box (`?q=`, key or title, also searches the archive) bring them back.
With `require_tracker_log = true` a done task without the tracker mark is never archived.

<a id="spec-board-inbox"></a>

**Inbox** ("Needs you") lists, grouped, with a count per group:

- **Review queue:** tasks in `review` or `test` with a checklist, across projects, the longest
  waiting first (by the last stage change), with accepted / all; a row opens the task dialog on its
  Review tab.
- **Waiting on you:** open tasks whose `waiting_on` is one of `owner_names` (config, default
  `owner`, `me`, `you`; compared case-insensitively after trimming).
- **Rule warnings:** tasks whose last stage move missed a working rule (the warnings in its
  `task.stage_changed` event), until the task moves again.
- **Not logged in the tracker:** done tasks without the work-log mark, only with
  `require_tracker_log = true`.
- **Unlinked hook sessions** ([§4](#spec-hooks-linking)), linking to the Integrations page.
- **Agent replies:** tasks with agent comments the owner has not marked read, with the newest
  reply's author and text.
- **Proposed by agents:** pending environment proposals ([§7](#spec-proposals)), linking to the
  project's Stands & access page.
- **Policy proposals:** pending policy proposals ([§7](#spec-policy-proposals)), linking to
  Settings → Policy.

Archived done tasks are left out. Task rows open the task dialog (`/p/<slug>?task=<key>`). The
sidebar badge shows the total number of rows (hidden at zero); badge and list refetch on events that
can change them (`task.*`, `session.seen`, `session.status_changed`, `comment.added`,
`comment.read`, `environment.proposed`, `environment.proposal_*`, `policy.proposed`,
`policy.proposal_*`, `review.step_*`), at most once per 2 s. With
nothing to show the page says "Nothing needs you right now."

<a id="spec-board-sessions"></a>

**Sessions** of a project are those its hooks placed in the project (by working directory) and
those bound to one of its tasks now; `/sessions` lists all sessions. Newest activity first, up to
200. Each row: status dot and hook status (working, waiting, idle, ended, or "no hook status"),
name, kind (desktop, hook or named), last hook activity and its age, working directory, "Open in
desktop" for a desktop session, the unlinked reason, and the bound tasks (each opens its task
dialog). The list refetches on session and binding events, at most once per 2 s.

<a id="spec-board-activity"></a>

**Activity** is the event log, newest first, 50 per page; "Load more" appends the next page
(`?before=<event id>`, also a plain link). Filters: project (`?project=<slug>`) and one event kind
(`?kind=<kind>`; an unknown kind is a 400). Hook status changes (`session.status_changed`) are
shown only when that kind is selected. Each row has the age, the kind, the task key or project as a
link, a short description (the same as `events_since`: `dev → test`, a report's summary, the
changed fields, a session's status change) and the actor. On the first page new events are
prepended live. Above the feed, **Actions on live** lists the newest 20 `live.action` events
([§7](#spec-journal)) for the project filter, refreshed live.

<a id="spec-board-live"></a>

**Live updates.** Every page subscribes to `GET /events` (`text/event-stream`; `?project=<slug>`
narrows the stream to one project). The board and the task page act only on their own project's
events; every page refreshes the Inbox badge. Each message has `id: <event id>`, `event: <kind>` and a small JSON `data` (kind, project, key, stage,
from, to, session, summary); the page refetches the affected fragment, so HTML is rendered only by
templates. A heartbeat comment is sent every 15 s. On reconnect the browser sends `Last-Event-ID`
and the server replays newer events (up to 5000); a client further behind, or ahead of the newest
event, gets `event: resync` and reloads. Waking from sleep (`visibilitychange`) or coming back
online forces a reconnect. A subscriber whose queue overflows (256 events) is dropped and catches up
on reconnect.

<a id="spec-board-markdown"></a>

**Agent text is untrusted.** Markdown is rendered with `markdown-it-py` with raw HTML disabled;
everything else is escaped. No CDN: htmx and its SSE extension are vendored.

<a id="spec-mcp"></a>

## 3. MCP contract

Streamable HTTP at exactly `/mcp` in the server process (official `mcp` SDK 2.x, `MCPServer`).
Every request needs `Authorization: Bearer <token>`. Tool results are short markdown for the model;
domain failures (unknown task, version conflict, invalid stage, rule refusal) are tool errors whose
message says what to do. Parameters and outputs of every tool: [MCP tools reference](mcp-tools.md).

<a id="spec-mcp-instructions"></a>

**Server instructions** (sent on connect) are the working contract, eleven numbered points: read
`project_snapshot` first (its first line names the rules); pick or create a task and pass `session`
(desktop id with `session_title`, else a name; `hook_session` if the session start gave one; don't
rebind without your `local_` id); update `next_step`, `waiting_on` and `progress` on every stage
change and report; report at the end of every stage with the configured first line, states and
sizes (taken from the effective global rules); set `waiting_on` to who decides when a decision is
needed; use `task_patch` for fields a cached schema lacks; fix what a `Warning (rules)` line names;
when a hook gives owner comments, handle them like a user message and call `comment_ack` (or answer
with `comment_add`); before touching a server, cluster or database read `environments_get` (the
level says what is allowed unasked; live is read-only unless the owner set otherwise), never paste
secrets, propose changes with `environment_propose`; before moving a task to review or test,
write the Owner check with `review_steps_set` and attach screenshots with `attach_file` (the
owner's decisions arrive as owner comments); start with a plan: `plan_set` from PLAN.md or your own
2-5 steps (S tasks: at least a todo), mark steps `in_progress` / `done` with `plan_step_update`,
done needs one line `as planned` or `deviation: why`.

<a id="spec-mcp-tools"></a>

**Tools.**

| Group | Tools |
|---|---|
| Projects | `project_list`, `project_upsert`, `project_snapshot` |
| Tasks | `task_list`, `task_get`, `task_create`, `task_update`, `task_patch`, `task_set_stage` |
| Sessions | `task_bind_session`, `task_unbind_session` |
| Reports and decisions | `report_add`, `decision_add` |
| Comments | `comments_list`, `comment_ack`, `comment_add` |
| Review | `review_get`, `review_steps_set`, `review_step_update`, `attach_file`, `attachment_caption` |
| Plan | `plan_get`, `plan_set`, `plan_step_update`, `plan_accept` |
| Environments | `environments_get`, `environment_propose` |
| Catch-up and rules | `events_since`, `rules_get` |
| Memory and documents | `decision_list`, `memory_add`, `memory_update`, `memory_search`, `memory_get`, `doc_list`, `doc_get`, `doc_update`, `doc_history`, `block_get`, `block_set` |

Read tools are annotated read-only, write tools non-destructive; none reaches outside relaybay.
Write tools take optional `session`, `session_title` and `hook_session`. `"-"` (or `""`) clears an
optional text field, because models cannot always send an empty string. `task_update`,
`task_patch` accept `expected_version` and fail instead of overwriting a newer version; otherwise the
last write wins.

<a id="spec-mcp-resources"></a>

**Resources.** `relaybay://project/{slug}` (same markdown as `project_snapshot`),
`relaybay://project/{slug}/environments` (same as `environments_get`),
`relaybay://rules` and `relaybay://rules/{project}` (same as `rules_get`),
`relaybay://doc/{project}/{doc}` (a document's current markdown), `relaybay://schema/task`
(the editable task fields as a table: name, type, description, how to clear). No MCP prompts yet.

<a id="spec-mcp-warnings"></a>

**Warnings.** A write that misses a rule in `warn` mode succeeds and its result carries one line per
unmet requirement, `Warning (rules): moving to test usually needs a report since the last stage
change.` In `strict` mode the tool fails with `Rules: … Nothing changed.`

<a id="spec-mcp-compat"></a>

**Compatibility policy.** MCP clients keep the parameter schema of a tool they already loaded, even
across server restarts. Therefore existing tools never gain required parameters; new behaviour comes
as new optional parameters or new tools, and `task_patch(task, fields={…})` reaches any editable
field with a stale schema. Removing a tool or parameter is announced with a deprecation warning for
one minor version first. MCP protocol: what the pinned SDK (`mcp>=2,<3`) negotiates. Minimum
supported Claude Code version: 2.0.0, checked by `relaybay doctor` (older releases are not tested;
unverified); http hooks need 2.1.63 (unverified, see [§4](#spec-hooks)).

<a id="spec-hooks"></a>

## 4. Hooks

Claude Code hooks report session activity without the agent's help. Endpoint
`POST /hooks/claude-code`, same Bearer token and Host/Origin rules as `/mcp`; the body is the hook's
input JSON. Bodies over 256 KB are dropped with `204`; a body that is not JSON gets `400`; every other
request answers `200` with JSON when there is something to tell the agent (the `SessionStart`
context, owner comments, a `Stop` block, [below](#spec-hooks-comments)) and `204` otherwise, without
waiting on anything else.

<a id="spec-hooks-events"></a>

| Hook event | Session status | Activity |
|---|---|---|
| `SessionStart` | working | `started (<source>)`; the answer adds "Your relaybay hook session: <id>. Pass it as `hook_session` in relaybay writes." to the agent's context (`hookSpecificOutput.additionalContext`) |
| `UserPromptSubmit` | working | `prompt` (the prompt text is never stored) |
| `PreToolUse`, `PostToolUse` | working | the tool name (the tool input is never stored; `PreToolUse` is also checked by the guard, and a call that touches live leaves a redacted summary in the journal, [§7](#spec-guard)) |
| `Notification` | waiting | the notification message |
| `Stop` | idle | `turn finished` |
| `SessionEnd` | ended | the session is unbound from every task it is bound to (history kept, `task.session_unbound` with `reason: "session_end"`), except after `/clear` or `/resume` (end reason `clear` or `resume`) and for desktop sessions, whose `local_…` id outlives the Claude Code process: the next `SessionStart` links the new Claude Code id to them ([below](#spec-hooks-linking)) |

Other events are ignored. Activity of a working session is written at most once per 5 s per session
(the latest wins); status changes are written at once, as `session.status_changed` per bound task.

<a id="spec-hooks-linking"></a>

**Linking a hook session to a relaybay session**, in this order:

0. Desktop session from the environment: the Claude desktop app runs Claude Code with its own
   session id in `CLAUDE_CODE_HOST_SESSION_ID`. The command hook adds it to `SessionStart` as
   `relaybay_host_session`, with `CLAUDE_CODE_SESSION_ATTENDED` as `relaybay_host_attended`; an
   http hook sends them as the `X-Relaybay-Host-Session` and `X-Relaybay-Host-Attended` headers
   (`allowedEnvVars`). When a desktop session with that id is known, the new Claude Code id is
   linked to it exactly (comments are delivered at once). While the desktop session is exactly
   linked to another live (not ended) Claude Code id, only the window itself moves it: an
   attended (`"1"`) `resume`, `clear` or `compact` start, e.g. after a restart whose old process
   sent no `SessionEnd`. A `claude -p` started from one of its tools inherits the desktop id but
   runs unattended, and a plain `startup` never moves a live link. An id another session's agent
   linked itself is never taken. Unknown or malformed ids are ignored.
1. Exact: a write passing `hook_session` (with `session`) links that Claude Code session id to the
   calling session; a row the hooks had created for it by guess hands over its status and is
   removed. An id that another live (not ended) session already linked exactly is not moved: the
   write succeeds and its response gets `Warning: hook_session <id> belongs to another session
   (<name>); not relinked.` `task_bind_session` never links a hook id (the caller may be binding
   another session); it answers with a note when one is passed.
2. Fallback on first sight: the one desktop session (not ended) bound to a task in the project
   whose `repo_path` contains the hook's `cwd`. A `SessionEnd` for an id no session holds is
   dropped.
3. Ambiguous (several candidates, the folder in several projects, or candidates already linked to
   other live hook sessions): nothing is guessed; the hook session stays **unlinked** with a reason
   and is listed on the Integrations page.
4. No desktop session in the project, or no project: a plain session named after the folder.

<a id="spec-hooks-down"></a>

**Never in the way when relaybay is down.** The command hook (`<python> -I -m relaybay.hook_client
--url … --token-file …`, standard library only) gives up after 0.3 s per network step, prints
nothing on failure (only the server's JSON answer on success), never uses a system proxy and always
exits 0. It prints the server's answer only when it is a `200` with a JSON object. http hook
entries use a 2 s timeout and get the same JSON body; failures do not block the session, but Claude
Code shows a non-blocking "hook error" notice. `relaybay hook` is the same forwarder behind the CLI.

<a id="spec-hooks-comments"></a>

**Owner comments through hooks.** Only a hook session that the agent linked itself (`hook_session`
on a write, step 1 above) gets comment text, and only for the tasks bound to its session now; a link
guessed from the folder, an unlinked, unbound or ended session gets none (`session.cc_exact`).
After the upgrade to `0008` no session gets comment text until its agent writes once with
`hook_session` (earlier links count as guessed).

- `UserPromptSubmit`, `PreToolUse`, `PostToolUse` answer
  `hookSpecificOutput.additionalContext` with the undelivered owner comments, one line each —
  `Owner comment on <key> (#<id>): <text>` — plus how to answer (`comment_add` with `in_reply_to`,
  or `comment_ack`). They are marked delivered to that session (`comment.delivered`) and never sent
  again by these events. When the task is bound to another session, owner comments delivered to
  the previous one and still unread are delivered again to the new one. One answer holds at most 8000 characters of comments, at least one comment;
  the rest go with the next events and the answer says how many are left.
- `Stop` answers `{"decision": "block", "reason": …}` while owner comments are delivered but
  unacknowledged, or undelivered (then delivered with the block), so the agent handles them first.
  relaybay blocks at most twice per turn per session, and not when `stop_hook_active` is set before
  it blocked in this turn (another hook continues the turn); its counter resets on
  `UserPromptSubmit` and whenever a `Stop` is allowed. One comment causes at most two Stop blocks
  in all, across turns (counted again from zero when it is delivered to a newly bound session);
  after that only the `SessionStart` line and `comments_list` name it. Otherwise the turn ends
  normally.
- `SessionStart` adds `You have <n> unread owner comment(s) on your relaybay tasks: call
  comments_list, then comment_ack or comment_add.` when there are any; that does not deliver them.
- An idle session receives the comment with its next prompt. The coordinator sees `comment.added`
  (with a 200-character excerpt) in `events_since` and can forward it.

<a id="spec-plan-hooks"></a>

**Todo capture.** `PostToolUse` of Claude Code's todo tools updates the session's todo
(`session_todo`): `TaskCreate` (tool_input `subject`, `activeForm`; the id from
`tool_response.task.id`) adds an item, `TaskUpdate` (`taskId`, `status`, or the status from
`tool_response.statusChange.to`; `subject` / `activeForm` if given) changes one (`deleted` removes
it; an id never seen is ignored), `TodoWrite` (older builds, `todos: [{content, status,
activeForm}]`) replaces the list. It is stored before the activity coalescing, so a change is never
lost. Unknown shapes are ignored; at most 50 items of 200 characters. The list belongs to the
session and its current Claude Code id: a new id (after `/clear`) starts a fresh one, and a list
written before the agent linked its hook session moves with the exact link. A change emits
`session.todo_updated` for each task bound to the session. The todo tools are off by default in
Claude Code 2.1.309 (desktop and CLI); the env `CLAUDE_CODE_ENABLE_TODO_TOOLS=1` turns them on.
`relaybay setup` does not write it.

<a id="spec-plan-reminders"></a>

**Plan reminders.** For an exactly linked session (`cc_exact`), per open task bound to it: the
current step (`Plan of <key> (accepted | draft, not accepted yet): step N/M '<title>' — <what and
why>.`, or `next step` when none is in progress, `all N steps done`), plus `Now doing:
<activeForm>.` for the todo item in progress; without plan steps the todo summary or, for a task
in dev with neither, the hint to write down what the agent is going to do. At most three tasks,
then one line on `plan_step_update`. Sent in `additionalContext` on `SessionStart` (after the
hook session line), on the `PostToolUse` of a todo change and every `plan_reminder_every`
`PostToolUse` events (default 30, `0` turns this one off; it repeats the current text). A todo
change never sends the same text twice in a row for a session (`SessionStart` is a new context and
resets it). Owner comments in the same answer get
the rest of the 8000-character budget.

<a id="spec-hooks-wake"></a>

**Wake-up hook (opt-in; verified with Claude Code 2.1.309).** With
`relaybay setup --apply --wake`, `Stop` also runs `<python> -I -m relaybay.hook_client --url
<base>/hooks/claude-code/wait --token-file <data dir>/token --wait` as a command hook with
`"asyncRewake": true` and a 1800 s timeout. Claude Code runs it in the background; it posts the
`Stop` input to `POST /hooks/claude-code/wait`, which holds the request up to 25 minutes. When an
undelivered owner comment exists for the exactly linked session while it is idle (and relaybay did
not block this turn's `Stop`), the server marks it delivered and answers `200` `{"wake": "<the same
lines>"}`; the hook writes them to stderr and exits `2`, which the hooks reference documents as
waking Claude with stderr as a system reminder. Otherwise it answers `204` and the hook exits `0`
silently: time up, the session not exactly linked, ended, a newer wait for the same session (each
turn starts one), or the hook process gone. relaybay down: exit 0 at once.

**Fallback risk:** a Claude Code build that ignored `asyncRewake` would run this hook in the
foreground and hold the end of every turn for up to 30 minutes. The hooks reference names no
version; Claude Code 2.1.309 was checked live (the turn ends at once, exit 2 starts a new turn), so
`MIN_ASYNC_REWAKE_VERSION` is 2.1.309 and `--wake` on an older or unknown version is refused unless
`--force-wake` is given too. `setup --check` and `doctor` name the verified version. If turns end
only after a long wait, run `relaybay setup --apply --no-wake`.

<a id="spec-setup"></a>

## 5. Setup and integrations

`relaybay setup` connects Claude Code on this machine. It writes only the user settings file
`~/.claude/settings.json` (read by the CLI and the desktop app's local sessions), and only on an
explicit command or button.

<a id="spec-setup-cli"></a>

| Command | Effect |
|---|---|
| `relaybay setup` (`--dry-run`) | Detection (`claude` on `PATH` and its version, the desktop app, whether http hooks are supported), the exact diff of the settings file and its plan hash. Writes nothing. |
| `relaybay setup --apply [--expect <hash>] [--mode command\|http] [--wake [--force-wake] \| --no-wake]` | Backs the file up, then adds relaybay's hook entries. `--wake` also adds the wake-up hook ([§4](#spec-hooks-wake); command mode only; refused without `--force-wake` on a Claude Code older than 2.1.309 or of unknown version), `--no-wake` removes it; without either an existing wake-up hook is kept. |
| `relaybay setup --remove [--dry-run] [--expect <hash>]` | Backs the file up, then removes only relaybay's entries. |
| `relaybay setup --check [--json]` | End-to-end check, one ok/FAIL line per item; `--json` prints a list of `{ok, name, detail}` and the exit code is non-zero when an item fails. |

Options: `--home`, `--port`, `--claude-home <folder>` (a folder holding `.claude/settings.json` and
`.claude.json`, for tests or another profile).

<a id="spec-setup-writes"></a>

**What apply writes.** One hook entry for each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `Notification`, `Stop`, `SessionEnd`, appended after existing hooks (the tool events
with matcher `*`). An installation without `PostToolUse` shows as "some events are missing" in the
check until apply runs again. With `--wake`, one more group in `Stop` for the wake-up hook.

- `command` mode (default): runs the interpreter that runs relaybay, `<python> -I -m
  relaybay.hook_client --url <base>/hooks/claude-code --token-file <data dir>/token`, 3 s timeout.
  Needs nothing on `PATH` and no environment variable. Shell quoting: single quotes on macOS and
  Linux for paths with spaces, `$` or backticks; double quotes on Windows (a path with `$` or a
  backtick is not supported there).
- `http` mode: Claude Code posts itself to `<base>/hooks/claude-code` with
  `Authorization: Bearer ${RELAYBAY_TOKEN}` and `allowedEnvVars`, 2 s timeout; `RELAYBAY_TOKEN` must
  be set in Claude Code's environment.
- **Marker:** an entry is relaybay's only when it is a command hook running `-m
  relaybay.hook_client` (or `relaybay hook`) with `--url <scheme>://<host>:<port>/hooks/claude-code`,
  or an http hook whose URL path is exactly `/hooks/claude-code` on a loopback address or the
  configured host. The wake-up hook is relaybay's when it is that command with the
  `/hooks/claude-code/wait` URL followed by exactly `--wait`. No extra property is written; similar
  user hooks are never claimed.
- Nothing else in the file is changed, reordered or removed; indentation and line endings are kept;
  a symlinked file is written through to its target. Running apply again changes nothing; a new port
  or data folder updates only relaybay's entries.
- **Backup** before every write: `settings.json.relaybay-backup-<YYYYMMDD-HHMMSS>` next to the file
  (`-<n>` appended if the name exists).
- Refused: a file that is not UTF-8, not valid JSON, or whose `hooks` are not in the documented
  shape. The token is never written into the settings.
- **Approved plan:** with `--expect <hash>` (and always from the board) the write happens only if
  the file and the plan still hash to what was shown; without it, only changes during the run are
  detected.
- **MCP registration** is never written: `~/.claude.json` belongs to Claude Code. Setup reads it to
  report whether a user-scope `relaybay` server is registered and otherwise prints the
  `claude mcp add --transport http --scope user relaybay <base>/mcp --header "Authorization: Bearer
  <token>"` command (`relaybay token` prints it with the token filled in).

<a id="spec-setup-check"></a>

**Check items:** relaybay server reachable with the same token (`/healthz` `token_id`); MCP
`initialize` accepted with the token; a synthetic hook event accepted; relaybay's hooks present in
the user settings for this base URL; in command mode, the hook's interpreter imports
`relaybay.hook_client`; in http mode, `RELAYBAY_TOKEN` set to the token in this environment; the
wake-up hook (an ok line naming the verified Claude Code version and the fallback risk, only when installed;
also in `doctor`); the MCP server registered for Claude Code.

<a id="spec-doctor"></a>

**`relaybay doctor [--json]`** (`--home`, `--port`, `--claude-home`) reads and never writes; unlike
`setup --check` it never creates the token and it reports Claude Code's files even when the server
is down. Items, in order: `relaybay` (version and interpreter), `Python` (at least 3.12), `OS`,
`data folder` (exists, writable, whether `config.toml` is there), `token file` (present, not empty,
on POSIX not readable by others), `Claude Code` (on `PATH`, version at least the minimum supported),
`desktop app` (informational), then the server items of the check (`relaybay server`, `MCP with the
token`, `hook endpoint`; only the first without a token or when nothing listens), `rules hash`
(the hash of the global rules from the settings; fails when the running server reports another one
in `/healthz`, i.e. it needs a restart), then the Claude Code items of the check (`hooks in user
settings`, `command hook runs` or `RELAYBAY_TOKEN for http hooks`, `MCP registered for Claude
Code`). Output: one `[ok]` / `[FAIL]` line per item, or with `--json` a list of
`{ok, name, detail}` (the shape of `setup --check --json`); exit code `1` when an item fails. Invalid
settings come out as one failed `settings` item.

<a id="spec-demo"></a>

**`relaybay demo --home <dir> [--force]`** seeds a made-up workspace into the data folder: projects
`acme-shop` (with a repository path) and `demo-api`, eleven tasks across all six stages with
descriptions, progress and reports, tracker badges (one logged done task), a stale next step, a
decision, a desktop session bound to a dev task and shown as working (one synthetic hook event),
and a task in review with a three-step checklist and two generated PNG screenshots, bound to a
session whose hook id `demo-review-hook-session` is linked exactly (so a simulated hook event
receives the owner's decisions); an M task in dev with an accepted plan (one step done as
planned with a report link, one done with a deviation, one in progress, one pending) and an S task
in dev without a plan whose session (`demo-todo-hook-session`, linked exactly) reported a
three-item todo.
`--home` is required. A database that already has a project or an event is refused (exit `1`);
with `--force` it is renamed to `relaybay.sqlite.before-demo-<YYYYMMDD-HHMMSS>` (with its WAL files)
and a new one is seeded. The token is not created; `relaybay serve --home <dir>` does that.

<a id="spec-setup-board"></a>

**Integrations page** (`/settings/integrations`): the same detection, plan (with a Hook type
select), diff, notes and check results; Apply, Remove and Run check buttons. Apply and Remove ask
for confirmation and write only the plan shown; if the file changed meanwhile they refuse and show
the new plan. The page also lists unlinked hook sessions with the reason.

<a id="spec-rules"></a>

## 6. Working rules

One structured document with a fixed set of keys:

| Key | Meaning | Default |
|---|---|---|
| `report_format` | `first_line` (pattern shown to agents), `states`, `sizes` (subsets of the built-in ones) | `state · size · gist`, all states, `S`/`M`/`L` |
| `stage_requirements` | per target stage, a list of `{kind = "report_since_stage_change"}`, `{kind = "field_present", field = "<task field>"}` or `{kind = "plan_accepted", unless_size = ["S"]}` | `dev`: an accepted plan unless size `S`; `test`: a report since the last stage change; `done`: `progress` |
| `description_template` | markdown template for new task descriptions | none |
| `guidance` | free-text markdown for agents | one short paragraph |

<a id="spec-plan-rule"></a>

**Work by plan.** `plan_accepted` is met when the task's plan is `accepted` or its size is in
`unless_size` (default `["S"]`; a task without a size needs the plan). In `warn` mode a move to dev
without it carries `Warning (rules): moving to dev usually needs an accepted plan (not needed for
size S).` plus `Write it with plan_set (2-5 steps), then ask for acceptance.` or, for a draft,
`The plan is not accepted yet: the owner accepts it on the board, the coordinator with
plan_accept.`; `strict` refuses the move. A move to dev of a task with no plan steps and no todo
also adds the MCP hint line `Hint: <key> has no plan and no todo yet: write down what you are
going to do (todo or 2-3 steps with plan_set).` (any size, not a rule warning).

<a id="spec-rules-merge"></a>

**Layers and merge:** built-in defaults ⊕ the global `[rules]` table of `config.toml` ⊕ a project
override (a row in `project_rules`, the same document as JSON). `report_format` merges per key,
`stage_requirements` per stage (a list replaces the earlier one, `[]` drops the stage's
requirements), `description_template` replaces, the config `guidance` replaces the built-in text and
a project's `guidance` is appended. Every layer is validated (unknown keys, stages, kinds or fields
name the key). An error in `config.toml` stops the server at start; a broken project override is
logged once and ignored, and `rules_get`, the resource and the snapshot line say "project override
ignored: <error>". There is no write tool for project overrides yet; the row is inserted by hand
([example](mcp-tools.md#working-rules)).

<a id="spec-rules-hash"></a>

**Identity:** no version numbers yet. The rules hash is the first 8 hex digits of SHA-256 over the
canonical JSON of the merged document plus `rules_mode`. `project_snapshot` starts with
`Rules <hash>: accepted plan before dev, report before test, progress before done (read
relaybay://rules/<project>)`.

<a id="spec-rules-modes"></a>

**Checks and modes:** requirements are checked on stage moves only (`task_set_stage`, `task_update`
/ `task_patch` with a stage, the board form). `rules_mode = "warn"` (default): the move succeeds and
the response, the board form and the `task.stage_changed` event carry the warnings.
`rules_mode = "strict"`: the move is refused with the reason and nothing changes. Report format is
not checked on `report_add` in this release.

<a id="spec-environments"></a>

## 7. Environments and access

Design: [Environments and access](design/0006-environments-and-access.md),
[Policy as data](design/0013-policy-as-data.md).

<a id="spec-policy"></a>

**Policy.** What agents may do on each kind of environment, what the guard counts as a change and
what counts as a secret are not fixed in the code: a policy (JSON, schema
`src/relaybay/policy/schema.json`, [policy reference](reference/policy.md)) defines them. The
effective policy of an environment is merged from these layers, a later one winning:

1. the built-in **preset** the global layer names (`default` when it names none);
2. `guard_mode` and `[permission_defaults]` from `config.toml` or `RELAYBAY_GUARD_MODE`, only
   when set (legacy; layer `config`);
3. the **global** layer, the file `policy.json` in the data directory (setting `policy_file`, a
   relative path is read from the data directory);
4. the **project** layer and 5. the **environment** layer, stored in the database
   (`policy_layer`).

Objects merge key by key; scalars and lists replace. The lists `guard.disable`, `guard.patterns`,
`secrets.disable` and `secrets.allow` may also be written `key+` (add; a pattern with an existing
`id` replaces it) and `key-` (remove by value or `id`), applied after `key`. `preset` counts only
in the global layer; `$schema` and `description` are ignored. Every value knows the layer that set
it (`preset:<name>`, `config`, `global`, `project:<slug>`, `environment:<slug>/<name>`). Without a
policy file and without the legacy keys the behaviour is the `default` preset, which equals
relaybay before policies.

| Preset | Levels | Guard | Secrets |
|---|---|---|---|
| `default` | local and test `full`, live `read` | `warn`, all rules | all detectors, no test secrets |
| `strict` | local `full`, test `ask`, live `none` | `block`, all rules | all detectors, no test secrets |
| `trusted` | `full` everywhere | `log` | all detectors; test secrets on local and test, not shown to agents |

Validation: JSON Schema 2020-12 plus checks the schema cannot express — `preset` only in the global
layer, custom pattern ids unique and not a built-in id, custom regular expressions (`command`,
`change`, `secrets.allow`) at most 200 characters, without back-references, without a quantified
group that holds a quantifier (`(a+)+`) or an alternation (`(a|aa)*`), with at most two
unbounded quantifiers (`*`, `+`, `{n,}`), and fast on hostile 4 KB inputs (all probes of one
pattern together within 50 ms). They run case-insensitively on the `regex` engine with a hard time
limit: when the guard or a secret check uses them they see at most 8 KB, and a call that takes
over 20 ms fails closed — a guard pattern counts as matched (the tool call looks like a change
for that pattern's capability), an allow pattern does not allow — logged at most once a minute
per pattern; the pattern stays on for the next call. A pattern that does not compile (including
conflicting flags such as `(?u)(?a)` or `(?V1)`) is refused on validation with its id, keeps the
last valid global file when it appears in `policy.json`, and, in a stored layer that skipped
validation, is skipped and logged, never an error in the guard. An error names the layer, the path and the problem, up to five at
once (`policy.json: guard.mode: 'loud' is not one of ['off', 'log', 'warn', 'block']`). A broken
global file stops `relaybay serve` (exit `2`) like a broken `config.toml`. The file is read again
whenever its modification time or size changes (no restart, no command); a file that breaks later
is logged once, shown on the Policy page, and the last valid version stays in effect. A broken
project or environment layer is logged once and ignored, and listed with the effective policy.

<a id="spec-environment"></a>

**Environment.** A project's stand: `name` (unique per project, case-insensitive; letters, digits,
spaces, dots, dashes, underscores), `kind` (`local` / `test` / `live`), optional `base_url` (`http://`
or `https://`), `sign_in_allowed` ("an agent may sign in and remember the login"; on by default for
local and test, always off for live: the flag is forced off and the database refuses it),
`login_state_path` (where the saved login state lives), `notes`, the agents' permission level
(below) and when and by whom it was last changed.

<a id="spec-access-map"></a>

**Access map, references only.** Each environment has access entries of a kind with string fields:

| Kind | Fields |
|---|---|
| `ssh` | `alias`, `host`, `port`, `user`, `key_path_ref` |
| `kubectl` | `context`, `namespace` |
| `db` | `type`, `host`, `port`, `port_forward`, `database`, `user`, `password_ref` |
| `gitops` | `repo`, `path`, `branch` |
| `logs`, `monitoring` | `url` (`http://` or `https://`) |
| `other` | any lowercase field names |

relaybay never stores a secret, only where it lives. Every text written to an environment, an
access entry or a proposal is checked (by detectors with stable ids that the policy can switch
off in `secrets.disable`, listed in the [policy reference](reference/policy.md)): a field whose
name contains `pass`, `pwd`, `token`,
`secret`, `key`, `credential` or `private` must be a reference — a path (`~/.ssh/acme_live`,
`C:\keys\x.pem`, a `*.pem`/`*.key`/… file name), an environment variable name (`$DB_PASSWORD`,
`${DB_PASSWORD}`, `%DB_PASSWORD%`, `env:DB_PASSWORD`, an upper-case name with `_` such as
`DB_PASSWORD`) or a vault path (`vault://…`, `op://…`, `keychain:…`, `ssm:…`; `pass:` / `file:` /
`sops:` only with a path that has text on both sides of a `/`, e.g. `pass:acme/db`); a field
whose name contains `user` or `login` must not hold `user:password`. Any field (notes too) is
refused when it contains a private key block, a GitHub, GitLab, Slack, OpenAI-style or AWS access
key, a JWT, or a value that is not a reference in one of these places: URL credentials
(`user:pw@`, also with an empty user `:pw@`); `--password`, `--passwd`, `--pass`, `--token`,
`--secret`, `--api-key` (with `=` or a space); `-u` / `--user user:password`; a `Bearer`,
`Basic` or `Digest` credential, `Authorization: Token …`; `sshpass -p`; `-p<password>` with
`mysql` / `mariadb` in the text; `-p <password>` with `docker login`; `-a <password>` with
`redis-cli`; an assignment whose name contains `pass`, `pwd`, `token`, `secret` or `key`
(`PGPASSWORD=…`, `API_KEY='…'`, `--from-literal=password=…`); `name: value` written as prose only
for the `pass` / `pwd` / `token` / `secret` / `api_key` family and values of 6 characters or more.
Quoted values count whole. The journal masks exactly these places, from the same table
(`_SECRET_SPOTS`), whatever the policy switches off. A value that is a reference as a whole
always passes, and so does a value, or a detected secret, that one of the policy's `secrets.allow`
patterns matches whole. The refusal names the field and the detector (`notes looks like it
contains a secret value (jwt)`) and never repeats the value. The checks of a write use the
effective policy of the environment (or, for a new environment and for proposals, of the
project). These are pattern checks: a secret in an unusual shape (e.g. in plain prose) can pass.

<a id="spec-test-secrets"></a>

**Test secrets.** When the policy allows them for the environment's kind
(`secrets.test_secrets.local` / `.test`; the schema has no `live` key and the service refuses them
on live whatever the layers say), an access entry may hold harmless test values: stored in its
fields JSON as `{"value": "...", "test": true}`, exempt from the secret checks, at most 20 per
entry, names in lowercase like other fields. They are entered on the board only (a "Test secrets"
box in the access form, shown when allowed or when the entry has some; `name: ***` keeps a stored
value, a removed line removes it, a form without the box keeps them); a proposal that carries one
is refused. The board shows them as `•••` with "(test secret)" and never renders the value.
`environments_get` lists only their names, unless the policy sets
`expose_test_secrets_to_agents: true`. An environment with test secrets cannot become live. The
journal masks every stored test secret value, whatever its length, in the commands it records.

<a id="spec-permissions"></a>

**Permission levels.** Per environment, `none` (do not touch), `read` (look only), `ask` (the
owner approves each action) or `full` (act as needed); optionally per capability `sign_in`, `ssh`,
`kubectl`, `db`, `deploy`. The level of a capability is, in order: the environment's override for
it, the environment's own level, the policy's `levels.<kind>.<capability>`, the policy's
`levels.<kind>.default`. Only a person changes levels, on the board, and some writes need a second
confirmation: on an environment that stays live, a level or override above `read` that is
higher than before; on an environment that becomes live (created or changed), a level above
`read` that is higher than what the project's policy alone gives live, or an environment policy
layer that weakens the guard compared with the project's (so a layer staged while it was a test
environment is confirmed now); on a live
environment whose kind changes, any capability whose effective level, the policy's included,
rises above `read` (live → test with the default preset turns `read` into `full`). Accepting a
proposal asks the same. Agents propose changes instead (below).

<a id="spec-environments-page"></a>

**Stands & access page** (`/p/<slug>/environments`, sidebar item under each project): one panel per
environment with its kind, effective level and where it comes from ("(default)" from the default
preset, "(policy: project:acme-shop)" from another layer, nothing when the environment sets it),
base URL, per-capability levels, sign-in flag, notes, who changed it when, and its access entries
(each with its own change time and author); forms to add, edit and delete environments and access
entries (fields typed as `name: value` lines) and to set the level and overrides. Writes are plain
form posts that need the browser session cookie and the CSRF token; a request that authenticates
with the Bearer token instead is refused (`403`) — a guard against an agent using the board, not
a security boundary, since anyone with the token can sign in. Deleting asks first. Pending agent
proposals are listed on top with **Accept** / **Reject** and refresh live.

<a id="spec-proposals"></a>

**Proposals.** `environment_propose(project, change, reason)` records a proposal and changes
nothing (`environment.proposed`). `change` names the environment (`environment`) and any of
`kind`, `base_url`, `notes`, `sign_in_allowed`, `login_state_path`, `level` (`default` clears it),
`overrides` (`{capability: level}`, `default` removes one) and `access` (entries to add); a new
environment needs `kind`. It is validated like a board write, secrets included, when proposed.
Pending proposals appear in the Inbox ("Proposed by agents") and on the Stands & access page.
Accepting applies the change in one transaction (with the same confirmation for raising live) and
records `environment.proposal_accepted`; rejecting records `environment.proposal_rejected`; a
decided proposal cannot be decided again.

<a id="spec-guard"></a>

**Guard (PreToolUse).** The guard mode, the levels and the mutating rules come from each
environment's effective policy: `guard.mode` `off` (not checked, not journaled), `log` (journal
only: no warning, no deny, no ask), `warn` (default) or `block`. The existing
`PreToolUse` hook sends the tool call; relaybay reads the `command` of a `Bash` call, or every string
in the input of an MCP tool (`mcp__<server>__<tool>`, except relaybay's own tools); only the first
8 KB are read, in at most 200 command segments, with at most 50 identifiers per environment. The
environments considered are those of the session's project and of the tasks bound to it, or, for
an unknown session, of the project whose `repo_path` contains the hook's `cwd`. An environment is
*touched* when the text names one of its identifiers (case-insensitive, at least 3 characters): the
`base_url` host, an ssh `alias` or `host`, a kubectl `context`, a db `host`, a gitops `repo`
(normalised: `git@host:org/repo.git` → `host/org/repo`). An identifier with a dot, dash or slash
counts anywhere as a whole token (not part of a longer name); a single word (`app`, `postgres`)
only where a host is expected, found by splitting each command segment into shell words once: the
destination of `ssh` / `sftp` as the command (after `VAR=…`, `sudo`, `env` …; options with a
value, attached or not — `-p 22`, `-p22`, `-oX=Y` — are skipped; with or without `user@`),
`[user@]host:` in the arguments of `scp` / `rsync` / `sftp`, the value of `--context`,
`--kube-context`, `-h` / `-hHOST` / `--host` (not for ssh), or a URL host — not in a commit
message, an `echo`, a file name or a package name. A mutating rule counts for a matched identifier
only when their capabilities agree (ssh → `ssh`, kubectl → `kubectl`, db → `db`, gitops →
`deploy`; the `base_url` host and MCP tool names take any rule); the level is the strictest of the
levels of those capabilities, and of all identifiers matched in one environment the strongest
decision counts.

Mutating heuristics (one table, `domain/guard.py`, each with a stable id the policy switches off
in `guard.disable`; they see text only). A rule applies when its
change follows its command in the same command segment (the text split at `|`, `;`, `&` and
newlines outside quotes), so `ssh app uptime; rm -rf build` is not a remote change:

| Rule (id) | Capability | Pattern (simplified) |
|---|---|---|
| kubectl change (`kubectl-change`) | `kubectl` | `kubectl … apply / create / delete / edit / patch / replace / scale / set / label / annotate / drain / cordon / uncordon / taint / exec / cp / rollout restart / undo / pause / resume` |
| helm change (`helm-change`) | `deploy` | `helm … upgrade / install / uninstall / delete / rollback` |
| remote change over ssh (`ssh-remote-change`) | `ssh` | `ssh …` with `rm`, `mv`, `cp`, `dd`, `tee`, `truncate`, `kill`, `reboot`, `shutdown`, `chmod`, `chown`, package managers, `sudo`, `crontab`, `systemctl start/stop/restart/reload/enable/disable/kill/mask`, `service … start/stop/restart/reload`, `docker rm/stop/restart/run/kill/exec/compose/pull`, `sed -i` |
| file copy to a host (`scp-to-host`) | `ssh` | `scp` / `rsync` whose last argument is remote (`host:path`); copying from a host is a read |
| file transfer session (`sftp-session`) | `ssh` | `sftp` |
| SQL change (`sql-change`) | `db` | after a database client (`psql`, `mysql`, `mariadb`, `sqlite3`, `sqlcmd`, `mongosh`, `redis-cli`, `clickhouse-client`, `cqlsh`, …; in MCP tool input also without one): `insert into`, `update … set`, `delete from`, `drop`, `alter`, `truncate`, `create table/database/index/user/role`, `grant`, `revoke`, `flushall`, `flushdb` |
| git push (`git-push`) | `deploy` | `git … push` |
| terraform change (`terraform-change`) | `deploy` | `terraform apply / destroy / import` |
| MCP tool by name (`mcp-tool-name`) | — | an MCP tool whose name has a word such as `create`, `update`, `delete`, `apply`, `deploy`, `exec`, `run`, `restart`, `scale`, `push` |

The policy adds its own rules after the built-in ones (`guard.patterns`: `id`, optional
`capability`, optional `command`, `change`, `description`; the reason says "this looks like a"
and the description), with the same segment logic: without `command` the `change` pattern alone.

Decisions per touched environment: `full` → nothing; `read` → nothing unless a rule matched;
`ask` → ask; `none`, or `read` with a rule → deny in `block` mode, a warning in `warn` mode; in
`log` mode every one of these is `log`. The strongest decision wins (deny > ask > warn > log). The answer is
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny" | "ask",
"permissionDecisionReason": …}}` — the reason (e.g. `relaybay guard: acme-shop live (live) is
read-only for agents; this looks like a remote change over ssh (matched 'prod-alias'). Ask the
owner, or propose a change with environment_propose.`) is shown to Claude for `deny` and to the user
in the permission prompt for `ask`. In `warn` mode the call goes on and `additionalContext` carries
the same reason ending `(warn mode: not blocked)`. relaybay never answers `allow`, which would skip
Claude Code's own permission prompt. Owner comments for an exactly linked session are added to
`additionalContext` of the same answer. A guard error is logged and the call goes on; relaybay down
or slow means no answer at all ([§4](#spec-hooks-down)).

**The guard is a safety net against mistakes, not a sandbox.** It knows only the names in the
access map; a host reached by an IP address, another alias, a script, an environment variable, a
tool other than Bash and MCP, or with the guard off is not recognised, and the heuristics both miss
changes and flag harmless commands.

<a id="spec-journal"></a>

**Journal of actions on live.** Every guarded tool call that touches a `live` environment is
recorded as a `live.action` event (any decision, including the calls that go on and `log`; not
with the guard `off`): project,
environment, the bound task of that project (the first, if several; none when the session has no
task there), tool name, the command — whitespace collapsed, obvious secrets masked (private key
blocks, known token shapes, `token <value>` and every place the secret check above refuses, quoted
values whole: `PGPASSWORD='***'`, `mysql -p'***'`, `sshpass -p ***`, `curl -u admin:***`, …), cut
at 200 characters — the decision (`allow`,
`log`, `warn`, `ask`, `deny`), the level and the matched rule. Activity shows the newest 20 as **Actions on
live** above the feed (with the project filter, refreshed live, linking to `?kind=live.action` for
all), and the task dialog and page show the task's own as **Actions on live**.

<a id="spec-environments-mcp"></a>

**MCP.** `environments_get(project)` and the resource `relaybay://project/{slug}/environments`
return the full map (the project's preset and guard mode, each environment with base URL, level
and its source, overrides, its own guard mode when it differs, sign-in flag and login-state path,
access entries with test secrets by name only unless exposed, notes, last change).
`project_snapshot` adds one line per environment after the description: `- live — live · agents:
read (default) · https://…`. `policy_get(project?, environment?)` returns the effective policy of
the global scope, a project or an environment: the layer chain and one line per value with the
layer that set it (`- guard.mode = "warn"  (preset:default)`), plus ignored broken layers. There is
no write tool besides `environment_propose` and `policy_propose`.

<a id="spec-policy-proposals"></a>

**Policy proposals.** `policy_propose(scope, patch, reason)` — scope `global`, `<project>` or
`<project>/<environment>` — records a proposal and changes nothing (`policy.proposed`, table
`policy_proposal`). The patch is merged into the layer's current JSON (objects key by key, any
other value replaces, `null` removes a key) and the result is validated when proposed and again
when accepted. Accepting stores the new layer (`policy.updated`, `policy.proposal_accepted`, with
the confirmation below); when the global file cannot be written after that commit, the proposal
goes back to pending (`policy.proposal_reopened`) and the error is shown. Rejecting records
`policy.proposal_rejected`; a decided proposal cannot be decided again.

<a id="spec-policy-page"></a>

**Settings → Policy** (`/settings/policy`, a tab next to Integrations): pending policy proposals
with Accept / Reject (refreshed live), the preset picker (writes `preset` into the global file,
keeping the rest), the effective policy of a chosen scope (global, each project, each environment)
as a table of value, setting and source layer, with the layer chain and ignored layers, a JSON
editor for that scope's layer (the global file, or the project or environment layer; empty removes
a database layer) with **Validate and preview** (the effective policy after the change and whether
it widens access on live) and **Save**, and the list of project and environment layers with
their raw JSON and who changed them when. Writes need the browser session cookie and the CSRF
token; a Bearer request is refused (`403`), as on Stands & access. A change widens agents' access
on live — and asks for a second confirmation, also when accepting a proposal — when a live level
of its scope (`levels.live.default` or a capability) rises above `read`, its guard mode gets
weaker (`block` → `warn` → `log` → `off`), it switches built-in guard rules off (`mcp-tool-name`
included), or it removes a custom pattern or changes one (same id, any other field different);
switching secret detectors off does not ask. An
environment layer of a local or test environment does not ask when saved, but when that
environment later becomes live, its layer is compared with the project's policy and the same
confirmation is needed (below the environment's own levels, see Permission levels). The
global file is written after the database transaction commits, atomically (a temporary file,
then a rename); a failed write is an error naming the file, not a server error. The preset picker
and accepted global proposals refuse to change a global file that is currently broken (the error
is shown) instead of overwriting it; the editor replaces it. Every saved layer records
`policy.updated` (scope, and what it widened).

<a id="spec-memory"></a>

## 8. Memory and context

Step 11.6a of [Memory and context](design/0008-memory-and-context.md): documents, decisions with
supersede chains, memory notes, blocks, full-text search and the Docs & rules page. Digests,
reader cursors, budgets and SessionStart injection (11.6b) and markdown import / export (11.6c)
are not built yet. **Visibility per role** (coordinator everything, a manager its project, a
session its task) is not enforced: every client with the token reads and writes everything.
Tools and arguments: [MCP tools](mcp-tools.md#memory-and-documents).

<a id="spec-memory-docs"></a>

**Documents.** A project document (`document`: name unique per project, title, kind as free text
such as `spec`, `status`, `brief`, `runbook`, `adr`, current version) keeps every version
(`document_version`: markdown body, author, reason, base version, the section a write replaced).
`doc_update` with `base_version` 0 creates it; a `base_version` other than the current version is
a conflict whose message holds the current version number and the current text (or section) to
merge. Writing the same text again writes no version. Section anchors come from ATX headings
(`#` … `######`) outside fenced code: lowercase, punctuation dropped, spaces and dashes collapsed
(`## Gift cards` → `gift-cards`, empty → `section`), a repeat numbered `-2`, `-3`. A section is
its heading and everything until the next heading of the same or a higher level. With `section`,
the new text replaces that section: starting with a heading it replaces the heading too,
otherwise only the text under it. A diff between any two versions is `difflib.unified_diff`.

<a id="spec-memory-decisions"></a>

**Decisions** carry `scope` (`task` when a task is set, else `project`), a source (`report`,
`event` or `comment` with its id, which must exist in the project, or `message` with an external
id, plus display text; or display text alone), `valid_from` / `valid_to` (when the fact held) and
`recorded_at` / `superseded_at` / `superseded_by` (when it was recorded or replaced; for decisions
from before 0.3.0 `recorded_at` and `valid_from` are their creation time). `supersedes` on
`decision_add` marks a current decision of the same project as replaced (it gets `superseded_at`,
`superseded_by` and `valid_to`); nothing is deleted. A decision that is already superseded cannot
be superseded again (the error names the newer one), so chains have no cycles. `decision.project_id`
stays required: facts for all projects are global memory notes.

<a id="spec-memory-notes"></a>

**Memory notes** (`memory`): scope `global`, `role` (with a role name such as `coordinator`) or
`project`; kind `user`, `feedback`, `project` or `reference`; title, body (fact, why, how to
apply), tags (lowercased, comma separated, each once), source as decisions, `review_at`, the two
time axes, `last_read_at` / `read_count` (bumped by `memory_get`), a `version` and the author.
`memory_add` without `op` first looks for current notes of the same scope (and project or role)
that share a title word (stop words and words of one or two letters left out): if there are any it
writes nothing and returns them with their title similarity; the caller answers with `op` `add`,
`update` (rewrites the target; needs `base_version`, the candidate's version, a newer one is a
conflict), `supersede` (a new note replaces the target, which keeps
`superseded_by` and `valid_to`) or `noop`. A **strong match** has a title similarity of at least
0.6 (Jaccard of the stemmed title words); `op=add` next to one needs `force_new`. `memory_update`
uses optimistic locking like documents; a superseded note cannot change.

<a id="spec-memory-blocks"></a>

**Blocks** (`memory_block`): a label unique per scope (global or one project), description, body,
`char_limit` (default 2000, at most 20000), `owner_only`, version. `block_set` with `base_version`
0 creates one; over the limit the write is refused with the count; a stale version is a conflict
with the current text. Owner-only blocks (and the mark itself) change only on the board.

<a id="spec-memory-secrets"></a>

**Secret scan.** Notes, decisions, blocks and documents (and their titles, tags, reasons and
sources) are checked with the secret value detectors of [§7](#spec-environments) under the
effective policy of the project (the global one for global notes and blocks); a match is refused
with the field and the detector id, never the value.

<a id="spec-memory-search"></a>

**Search.** One FTS5 table `search_index` (columns kind, ref, project, title, body and a stemmed
shadow column; tokenizer `unicode61 remove_diacritics 2`) covers current document versions,
decisions, notes, reports and comments. The service creates it at start when SQLite has FTS5 and
keeps it in step in the same transaction as each write; at start it is rebuilt when its row count
differs from the items it covers (a new index, an upgrade). Query words become quoted prefix terms
OR'ed with their Snowball stem (English, or Russian for words with Cyrillic letters), all words
required; ranking is bm25 with a title hit weighing most. Without FTS5 (the table cannot be
created) nothing fails: search runs as `LIKE` substring matches of each word or its stem over the
same columns (case-insensitive for ASCII letters only). A hit names its line: the line holding the
most query words. Search with a project covers its items plus global and role notes; superseded
notes and decisions are left out unless asked for.

<a id="spec-memory-page"></a>

**Docs & rules page** (`/p/<slug>/docs`): a search box over the index; the project's documents
(title, name, kind, version, last change); decisions (current, superseded ones collapsed with
"superseded by #n"); memory notes of the project plus global and role notes (superseded ones
collapsed); blocks of the project and global ones, each with an edit form for the owner (text,
description, limit, owner-only; CSRF and the board cookie like other forms, a refused write keeps
the typed text); the effective rules (`rules_get` rendered). Documents, decisions and notes
refresh live on `document.updated`, `decision.added`, `memory.*`; blocks and search results stay as
the owner left them. A document page renders the markdown safely with heading anchors, a list of
sections, the history with a link per version and a diff between any two versions (escaped lines,
added and removed lines marked).

<a id="spec-security"></a>

## 9. Security model

relaybay is a single-user local server. The security layers, in request order:

<a id="spec-security-bind"></a>

1. **Bind address.** `127.0.0.1` by default. Any other address (`--host`, LAN or phone access)
   prints a warning; the names used must be added to `allowed_hosts`. No built-in TLS. The Docker
   image binds `0.0.0.0` inside the container (`RELAYBAY_HOST`); its port is meant to be published
   on the host's loopback only (`-p 127.0.0.1:8765:8765`), where requests carry `Host: 127.0.0.1`.
2. **Host allow-list** (DNS rebinding): every request's `Host` must be `127.0.0.1`, `localhost`,
   `[::1]`, the bound host or a configured `allowed_hosts` name, otherwise `403`.
3. **Origin check:** a foreign `Origin` is refused (`403`) on every non-GET request and on every
   request to `/mcp`, `/api` and `/hooks`. `/mcp` also has the MCP SDK's own DNS-rebinding
   protection with the same lists.
4. **Token.** 32 random bytes (URL-safe), created on first run in `<data dir>/token`, written
   atomically with mode `0600` (on Windows the user profile ACL protects it; a POSIX file readable by
   others logs a warning). Required as `Authorization: Bearer` on `/mcp`, `/hooks` and the reserved
   `/api` prefix. `relaybay token --rotate` replaces it (restart the server).
   `/healthz` is public and returns `status`, `version`, `token_id` (a 12-hex-digit HMAC
   fingerprint that reveals nothing about the token) and `rules_hash` (the global rules the server
   started with, [§6](#spec-rules-hash)).
5. **Browser session.** `/login?token=…` (or the sign-in form) sets an `HttpOnly`, `SameSite=Strict`
   cookie (`Secure` over https, one year) derived from the token and redirects, so the token leaves
   the address bar; the access log redacts the `/login` query. Unauthenticated page visits redirect
   to `/login?next=…` (only local paths are accepted as `next`). A Bearer header also works for the
   board's URLs, except the environment writes ([§7](#spec-environments-page)).
6. **CSRF.** Cookie-authenticated non-GET requests must be same-origin (`Origin`, or
   `Sec-Fetch-Site: same-origin`); board forms carry a CSRF token derived from the token; the sign-in
   form uses a double-submit nonce.
7. **Response headers:** `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
   object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
   `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.
   htmx runs with eval and script tags disabled. Links to `/static/` carry `?v=<content hash>`,
   so a browser fetches a changed script or stylesheet after an upgrade instead of a cached one.
8. **Untrusted content:** agent markdown is rendered with raw HTML disabled; hook prompt text and tool
   input are never stored, except the redacted 200-character summary of a call that touches a live
   environment ([§7](#spec-journal)); hook bodies are capped at 256 KB.
9. **No secrets at rest:** environment, access and proposal writes refuse obvious secret values
   ([§7](#spec-access-map)).
10. **Stored files:** screenshots are kept only when their magic bytes say PNG, JPEG, WebP or GIF
    (no SVG), under random names in the data folder, read by `attach_file` only from allowed
    folders after resolving symlinks and `..` ([§1](#spec-attachments)). `GET /files/<id>` needs
    the board cookie (or the Bearer token), answers with the stored `Content-Type`,
    `Content-Disposition: inline`, `nosniff` and the CSP above, so a file is never run as a page.

<a id="spec-security-forms"></a>

**Full-page forms** (Stands & access, Settings → Policy, Settings → Integrations without htmx)
answer a successful write with `303` to their page (POST → redirect → GET), so a refresh does not
send the form again; the outcome ("Environment saved.", "Saved the acme-shop policy.", the check
results) rides in a short-lived `HttpOnly`, `SameSite=Strict` cookie (`relaybay_flash`, 60 s) that
the page shows once and deletes. A refused write still renders the page with the reason, and the
"confirm raising live" step is a rendered page whose confirmed save redirects.

<a id="spec-no-telemetry"></a>

**No telemetry.** relaybay sends nothing anywhere: no analytics, no update checks, no outbound calls
at runtime, no CDN. The only network clients in the code talk to relaybay's own local server
(`relaybay token`, `relaybay setup --check`, the command hook), and they bypass system proxies.

<a id="spec-threat-model"></a>

**Threat model.** Protected against: other websites in the user's browser (Origin, CSRF, SameSite,
Host allow-list), other users on the machine (token file permissions), other devices on the network
(loopback bind by default), agent output used as an attack on the board (no raw HTML, CSP).
**Non-goals:** relaybay is not a sandbox and does not restrict what agents do on the machine (the
guard, [§7](#spec-guard), only catches mistakes it can recognise); anyone
with the token has full access (there are no accounts or roles); a LAN bind without a TLS proxy
exposes the token and cookie to the network; a process running as the same OS user can read the
token file. The identity passed as `session` is for display, never for authorisation.

<a id="spec-config"></a>

## 10. Configuration

Settings come from defaults, then `config.toml` in the data directory, then `RELAYBAY_*` environment
variables, then command-line flags. The data directory (per-user data dir via `platformdirs`, or
`RELAYBAY_HOME` / `--home`) holds `relaybay.sqlite`, `token`, the optional `config.toml`, the
optional global policy `policy.json` ([§7](#spec-policy)) and `attachments/` (screenshots,
[§1](#spec-attachments)). An invalid value stops the start with a
message naming the key (an invalid policy file: its path and the problem). Keys, variables and
flags: [configuration reference](reference/configuration.md); policy keys:
[policy reference](reference/policy.md).

<a id="spec-distribution"></a>

**Distribution.** The version lives only in `pyproject.toml`; `relaybay --version`, `/healthz` and
the MCP server info read it from the installed package. The wheel and sdist carry the templates,
static assets and migrations; `python -m relaybay` is the same as `relaybay`. The Docker image
(`ghcr.io/codenator81/relaybay`, `linux/amd64` and `linux/arm64`) runs `relaybay serve` as a
non-root user with `RELAYBAY_HOME=/data` (a volume), `RELAYBAY_HOST=0.0.0.0`, port 8765 and a
`/healthz` health check.

<a id="spec-data"></a>

## 11. Data model and migrations

SQLite in WAL mode with `foreign_keys=ON` and a 5 s busy timeout; one process writes. Migrations are
numbered SQL files applied in order at start, each atomically, tracked by `PRAGMA user_version`;
existing databases upgrade in place. A migration whose first line is
`-- relaybay:foreign-keys-off` rebuilds a referenced table: it runs with foreign keys off and is
committed only when `PRAGMA foreign_key_check` finds no violation that was not there before; the
error names the tables. Dangling keys an older database already had are logged and kept.

| Migration | Adds |
|---|---|
| `0001_init` | `project`, `task`, `report`, `decision`, `session`, `task_session` (binding history), `event` |
| `0002_task_description_progress` | `task.description`, `task.progress` |
| `0003_tracker_and_session_alias` | `task.tracker_*`, `session_alias` |
| `0004_next_step_freshness` | `task.next_step_at`, `task.stage_changed_at` (backfilled, nothing marked stale on upgrade) |
| `0005_tracker_history` | `tracker_entry` (backfilled one entry per task with a key) |
| `0006_project_rules` | `project_rules` (per-project rules override) |
| `0007_hook_sessions` | `session.cc_session_id` (unique), `session.activity_at`, `session.unlinked_reason` |
| `0008_comments` | `comment` (task, author kind and name, author session, body, `in_reply_to`, created / delivered / read times, `delivered_to_session_id`); `session.cc_exact` (0 for links made before it) |
| `0009_environments` | `environment` (project, name, kind, base URL, sign-in flag — never on live, login state path, notes, level, overrides JSON, position, changed at / by), `environment_access` (environment, kind, fields JSON), `environment_proposal` (project, change JSON, reason, proposed by, session, status, decided at / by) |
| `0010_policy` | `policy_layer` (project, environment or none for the project layer, body JSON, changed at / by; one per scope), `policy_proposal` (scope, project, patch JSON, reason, proposed by, session, status, decided at / by) |
| `0011_review` | the `review` stage (the `task` table rebuilt with the wider stage check, rows and ids kept), `review_step` (task, position, title, details, status, decided at / by, created by), `attachment` (task, step, kind, stored name, original name, type, size, SHA-256, caption, created by / at), `comment.step_id`, `comment.attachment_id` |
| `0012_memory` | `decision.scope`, `source_kind` / `source_ref` / `source_text`, `valid_from`, `valid_to`, `recorded_at`, `superseded_at`, `superseded_by` (backfilled: scope from `task_id`, `recorded_at` and `valid_from` from `created_at`); `document`, `document_version`, `memory`, `memory_block`. Additive only. The FTS5 table `search_index` is created by the service at start, not by the migration ([§8](#spec-memory-search)) |
| `0013_plan` | `task.size`, `task.plan_status`, `task.plan_accepted_at`, `task.plan_accepted_by`; `plan_step` (task, position, title, details, status, done at, link, outcome, conformance, created by), `plan_revision` (task, revision, body JSON, author, reason), `session_todo` (session, Claude Code id, item id, subject, status, active form, position). Additive only |

The archive is computed, not stored. Events are kept forever. Table details:
[architecture](architecture.md#data-model).

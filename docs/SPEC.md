# relaybay specification

How relaybay behaves **now**: the concepts and every shipped feature. This page never describes
plans; upcoming work lives in the [roadmap](ROADMAP.md) and the [design documents](design/index.md).
When a feature ships, its final behaviour moves here in the same pull request.

relaybay is a coordination board next to the Claude desktop app and terminal sessions: the
sessions report through MCP tools and Claude Code hooks, the owner reads, answers, approves and
accepts on the board, and relaybay starts no agent ([design
0025](design/0025-board-with-the-desktop.md)).

Version covered: `0.16.0`. Anchors of the form `#spec-…` are stable and may be
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
[11. Data model and migrations](#spec-data) ·
[12. Backlog](#spec-intake) ·
[13. Desktop program](#spec-desktop)

<a id="spec-concepts"></a>

## 1. Concepts

<a id="spec-project"></a>

**Project.** A unit of work: `slug` (lowercase letters, digits, dashes), `name`, optional
`description` and its repositories. Created or updated with `project_upsert`.

<a id="spec-project-repos"></a>

**Repositories.** A project has any number of repositories (`project_repo`), in order: a name
(letters, digits, `.`, `_`, `-`; unique per project, case ignored; without one, the path's last
folder), a path stored exactly as written (the server never lists it) and an optional
kind `app`, `web`, `api`, `lib` or `other`. `project_upsert(repos=[{name, path, kind}, …])` writes
the whole list, matched by name, so a repository that stays keeps its tasks' links; `[]` removes
them all. The owner edits them on the project's **Settings** page ([§2](#spec-board-repos)).
`repo_path` stays for older readers: it is always the path of the first repository, written in the
same transaction as every change of the list (moving another one first changes it; removing the
last one leaves it as it was). `project_upsert(repo_path=…)` without `repos` sets the first
repository's path, or adds one named after the folder when the project has none (also with the
same path as the kept `repo_path`); passed with `repos` it must equal the first one's path.
Migration 0029 gave every project with a `repo_path` one repository named after its last folder
as a name must look (each run of other characters becomes `-`, leading and trailing `-` and `.`
dropped, at most 60 characters); a bare drive or root, or a name left empty or not starting with a
letter or digit, takes the project's slug. Matching a session's folder to a project ([§4](#spec-hooks)) still uses `repo_path`.

<a id="spec-task-code"></a>

**A task's code.** A task links the repositories of its project it touches (`task_repo`, one per
repository, with who added it and how: `auto` from the hooks, `agent` over MCP, `owner` on the
board) and its pull requests (`task_pr`: an http or https link, unique per task, the number taken
from a `/pull/<n>`, `/pulls/<n>`, `/merge_requests/<n>` or `/pull-requests/<n>` path, optionally
tied to one repository). `task_update` / `task_patch` take `repos` (names to add, `-name` removes
one; an unknown name fails with the project's names) and `prs` (links to add, each optionally after
a repository name, `web https://…`; `-<url>` removes one). Without a name, a link whose path names
one of the project's repositories (`…/<owner>/<repo>/pull/1`, GitLab's `/-/merge_requests`) is tied
to it. A pull request tied to a repository also links that repository. Removing a repository from
the project removes the tasks' links to it; their pull requests stay, tied to none. Each write is
one `task.updated` event with `changes: {"repos": {"added": […], "removed": […]}, "prs": {…}}`; the
task's version does not change for links alone. `task_get` shows them under `## Code`.
A `repos` entry `name <absolute path>` (the name, a space, a POSIX or Windows absolute path; up to
500 characters; nothing on disk is read) also sets the **worktree** of that repository for the
task (`task_repo.worktree`, migration 0060): the folder the task works in there; `name` alone
keeps it, a relative path is refused. The event adds `"worktree": {"<name>": "<path>"}` to
`changes.repos`. The hooks set it too ([§4](#spec-hooks-repos)). `task_get` shows it on the
repository's `## Code` line (`· worktree `<path>``), `task_context` as a `Worktree: <name>: <path>
(branch …)` head line, the board on the Code row ([§6](#spec-board-code)).

<a id="spec-task-blockers"></a>

**Blockers.** A card can wait for other cards: `task_update` / `task_patch` take `blocked_by`
(`task_block`, migration 0061, one row per pair with who added it and when), a list of card keys of
any project on the board (`acme-shop-12`, case does not matter) to add; `-key` removes one. Refused
in the same write, which then changes nothing: a key no card has (the error names it), anything
that is not a key (a link, words), the card itself, and a cycle: a blocker that already waits for
the card, directly or through other cards (the error lists the chain, `a is blocked by b is blocked
by c`). A blocker **blocks** while it is in `plan`, `dev`, `review` or `test`; at `live` or `done`
(or archived) it no longer does, and moved back to an earlier stage it blocks again. That is read
from its stage on every read: a stage move writes nothing on the links. `task_create` takes no
`blocked_by`. Each write is one `task.updated` event with `changes.blocked_by` `{"added": […],
"removed": […]}`; the task's version does not change for links alone. `task_context` has a `##
Blocked by` section when the card has blockers, a line each `- <key> · <stage> · the title · <board
link>` (`<board>/p/<project>?task=<key>`) with ` (no longer blocks)` for one at live or done, and a
`Blocks: <key>, <key>` head line naming the cards not yet done that the card blocks; `task_get` the
same `## Blocked by` section and a `blocks` line. The board shows a `blocked by <key> · <stage>`
chip on the card for each blocker that still blocks (a link to that card), and the task dialog a
**Blocked by** row (every blocker with its stage; the ones that no longer block muted) and a
**Blocks** row. Deleting a card (the archive purge, a project delete)
takes its links in both directions along, and its export holds them.

<a id="spec-merge-order"></a>

**Merge order.** The project's cards in `test` go to the main branch in this order: a card after
the test cards that block it ([blockers](#spec-task-blockers)), otherwise by the owner's order of
the test column (`task.rank`, then the card id), because the blockers are what makes one merge
depend on another and the column order is the owner's own choice for the rest. A cycle cannot be
written; if one is met all the same, the remaining cards follow the rank. Each line is
`1. acme-shop-4 Checkout · repos web, api · PR #12, #13 · waits for demo-api-2 · dev` (repos, pull
requests and waits only when there are any; at most three pull requests with `+n`). Warnings follow the list:
`repos overlap: <repo> — <key>, <key>` for a project repository linked to two or more test cards
(in merge order), and `<key> waits for <key> (<stage>)` for a blocker that still blocks and is not
in test. It is shown only with two or more cards in test: in `project_digest` and
`project_snapshot` as `## Merge order (test)`, and on the board as a fold of the test column
([§2](#spec-board-merge-order)). Read once per project: the test cards, their repository links
and pull requests, their blockers (code: domain/merge_order.py:merge_order,
service.py:Service.merge_order).

<a id="spec-areas"></a>

**Areas** ([design 0024](design/0024-card-modes-thread-and-areas.md#areas)). What part of a project a
task belongs to ("web shop", "mobile app", "api"): one area per task or none, from a list per project
(`project_area`) that grows from use. A name is stored with outer spaces trimmed and inner runs of
spaces made one, at most 32 characters, and matches an area of the project without regard to case,
so "Web shop" and "web shop" are one area and the stored spelling stays. A name the project does not
have yet is added to its list (at most 24 areas a project; past that the write fails with the
existing names), last, with the colour of its place in a palette of eight (`blue`, `green`, `amber`,
`violet`, `teal`, `rose`, `slate`, `orange`), which the owner may change. `area` is a task field
([below](#spec-task)): `task_create(area=…)`, `task_update(area=…)`, `task_patch(fields={"area": …})`
and the board form set it; `-`, `""` or null clears it. A new area is a `project.updated` event with
`changes.areas.added`; the task's own change is `task.updated` with `changes.area` (the name, or null).
Over MCP a new name adds a line to the answer: `Area '<name>' was new: added to <slug>. Its areas now:
…`, so an agent sees a near-duplicate it just made. The owner renames, recolours, reorders and deletes
areas on the project's Settings page ([§2](#spec-board-areas)); renaming an area to another
area's name merges the two (its tasks move, it is deleted); deleting one clears it from its tasks,
each with a `task.updated` event `changes: {"area": null}` and its version raised, so its history
keeps what it was. A rename and a merge also raise the version of the area's tasks (each a
`task.updated` with `changes.area`, the new name), so a form opened before them is refused as a
conflict instead of bringing the old name back; a colour change does not. Saved filters keep an area
by name: they follow a rename or a merge and lose the area with a delete.

<a id="spec-task"></a>

**Task.** Belongs to a project and has a key `<slug>-<n>` (`acme-shop-12`), numbered per project.
Fields: title, stage, stage note, waiting on, next step, branch, document path, description,
progress, priority, size (`S` / `M` / `L`, [below](#spec-plan-size)), area
([above](#spec-areas)), the external tracker fields, deploy notes, cancelled and a `version` that
grows with every write. The editable fields come from one list (`domain/task_fields.py`) that
drives write validation, the `relaybay://schema/task` resource and the "Editable fields" line of
`task_get`.

<a id="spec-task-deploy"></a>

**Deploy notes and cancelled** (migration 0060). Set through `task_patch` (and the board form)
only; `task_update` has no parameters for them. `deploy_notes` (markdown, up to 20,000
characters) says what a deploy of the card needs: a migration, a feature flag, a script, the
rollback. `deploy_done` (boolean) marks those steps as carried out; a write that changes
`deploy_notes` clears it in the same write unless `deploy_done` is passed with it (the board form
counts its checkbox only when it was changed). `cancelled` is the reason the card was given up on
(up to 2,000 characters): setting it stamps `cancelled_at`, `'-'` clears both. The card is not
moved: the answer adds the line `Hint: <key> is marked cancelled and stays in <stage>: …` and the
card goes to done by an ordinary stage move. A move to done of a card that is not cancelled, with
deploy notes not marked done, adds a hint ([§8](#spec-change-documents)). `task_context` shows `Cancelled: <reason> (<date>)`
and `Deploy notes: done|not done yet: <notes>` head lines; `task_get` a `cancelled` line and a
`## Deploy notes (done|not done yet)` section; the board a **Cancelled** row and a `cancelled`
chip on the card, and a **Deploy notes** panel with its state. A `deploy` approval request shows
the card's deploy notes in its answer (or says the card has none) and on its board item
([below](#spec-approvals)).

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
`review_step_update` (text, or `status: open` to ask for a new look after a fix; `remark`, markdown
up to the comment limit, adds the calling session's comment tied to the step, `comment.step_id`,
shown in the step's thread on the board, and leaves the step's status as it is). Only the owner
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

<a id="spec-plan-living"></a>

**Living plan.** `plan_step_add(task, title, details?, reason)` appends a pending step; `reason` is
required. The plan's status stays as it is: on an accepted plan the step is marked "added after
acceptance" (`added_after_acceptance_at`, `added_by`, `added_reason`), shown on the step in the Plan
tab and in the history diff (`+ 4. Undo button (added after acceptance)`); on a draft (or no plan,
which becomes a draft) it is a plain append. A `plan_revision` with the reason
(`step 4 added after acceptance: <reason>`) and `plan.step_added` (position, title, reason, after_acceptance, moved_to_dev). A
`plan_set` that renames the step drops the mark. **The stage follows the plan**
([§6](#spec-plan-rule)).

**Closed-plan warning.** On a task whose plan steps are all done, `Warning (rules): the plan is
closed (N/N done) while work goes on: add a step with plan_step_add or reopen one.` joins the MCP
answer only once a report of the task came strictly after the plan closed (the newer of the
newest step's `done_at` and the newest plan revision, so a `plan_set` that drops the last open step
also closes it); the report and the progress that close the plan never warn. Then it joins a report
whose state is `changed` in any open stage, or `verified` with the task in `dev`, and a
`task_update` / `task_patch` that changes `progress` on a task in `dev`. It is a warning in `strict` mode too, never a refusal. `plan_step_add` on a
task in `done` is refused (`the task is done: reopen it (task_set_stage) before adding steps.`);
in `live` it is allowed and the answer adds `<key> is in live: the step does not move it.`.

<a id="spec-plan-status"></a>

**Plan status** lives on the task: none while there are no steps, `draft` once written,
`accepted` (with who and when) after the owner accepts it on the board or the coordinator calls
`plan_accept` (refused for the session bound to the task). A change of the step titles or their order turns an accepted plan back to `draft`
(the `plan_set` answer says so); step statuses and "what and why" edits do not. Every plan write
(set, step add, step update, acceptance) adds a `plan_revision` row with the whole plan as JSON, its author
and a reason. Events: `plan.set` (steps, status, previous status, whether the titles changed,
reason), `plan.step_added`, `plan.step_updated` (position, title, status, previous, conformance,
outcome), `plan.accepted`, `plan.acceptance_delivered` (the owner's acceptance told to the bound
session through its hooks, [§4](#spec-hooks-comments)).

<a id="spec-plan-draft-hint"></a>

**Draft hint.** A task in `dev` whose plan is a draft and whose newest plan revision is at least
`plan_draft_hint_hours` old (default 1, `0` off) is named at the end of `portfolio_digest`,
`project_digest` and `task_context` when the caller is a coordinator session (named in
`coordinators`, checked before the call links its hook session; `project_snapshot` has no caller
identity and never carries it): `Hint: acme-shop-12's plan is a draft for 3 h: accept it (plan_accept) or ask for
changes.` (whole hours), oldest draft first, at most five lines and then `…and N more drafts.`.
Never a line for the agent.

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
removed task). Events: `attachment.added`, `attachment.updated` (caption, or a new flattened
image: [Annotations](#spec-annotations-flatten)), `attachment.removed`.

<a id="spec-annotations"></a>

**Annotations.** In the screenshot viewer the owner marks up a screenshot and drops numbered pins
([design 0009](design/0009-screenshot-annotations.md)); the agent gets the pins with coordinates and
a flattened PNG.

<a id="spec-annotations-model"></a>

*Model.* An `annotation` belongs to a screenshot: kind `rect`, `arrow`, `freehand` or `pin`, a colour
from a fixed list of five, a stroke width (a fraction of the image's width, at most 0.05) and its
geometry as JSON in **fractions of the image size**: rect `{x, y, w, h}`, arrow `{x1, y1, x2, y2}`,
freehand `{points: [[x, y], ...]}`, pin `{x, y}`. Coordinates are numbers in 0..1; up to 0.01 past
an edge is held to the edge, anything else is refused. A pen line takes at most 5000 points. On save
a longer line is thinned along its length to at most 500, keeping each slice's farthest point so a
fine zigzag keeps its height, and then simplified with a fixed tolerance of 0.001, so only a line
that really is straight ends with 2 points. A screenshot holds at most 200 live annotations. Only a pin has a
comment (a string or null, up to 2000 characters) and a thread of replies (`annotation_reply`: author owner or agent).
Pin numbers are per screenshot and never reused. Deleting is soft: the row keeps `deleted_at`, the
number stays taken and Undo brings it back with its thread. Comment and reply texts are cleaned like
comments and pass the secret scan ([§8](#spec-memory)). Updating a pin needs `body` in the request:
`null` or an empty string clears the comment, `{}` is refused. Only the owner draws, on the board; an agent
only answers. Events: `annotation.added`, `annotation.updated`, `annotation.removed`,
`annotation.replied`. They refresh the open viewer and nothing else.

<a id="spec-annotations-flatten"></a>

*Flattened image.* The vector data is the source of truth. On "Send to agent" the viewer draws the
image and the markup at the image's own resolution on a canvas and uploads the PNG (`POST
/files/<id>/annotated`, the raw image as the body). The upload passes the board's cookie and CSRF
checks, must be a PNG by its magic bytes and at most `attachments_max_mb`. Its name comes from the
server. It lies beside the original, which never changes (`attachment.annotated_name`,
`annotated_size`, `annotated_sha256`); the previous flattened file is removed after the commit (one
that cannot be removed now is logged, the upload still succeeds and the next start removes it). Its
size counts in
`attachments_task_max_mb`, and the startup sweep keeps it. `GET /files/<id>/annotated` serves it
behind the board cookie, revalidated on every read because the URL stays when the file changes.
Removing the screenshot removes both files. Event: `attachment.updated`.

<a id="spec-annotations-delivery"></a>

*Delivery.* "Send to agent" posts one owner comment through the comment path
([below](#spec-comments)), tied to the step (`comment.step_id`) and the screenshot
(`comment.attachment_id`): "Screenshot 3 of step 2 — 2 pins: #1 (x 0.42, y 0.18, annotation 12)
'Button text is cut off'; #2 (…) 'Wrong currency'. Also marked: rectangle (…). Annotated image:
relaybay://attachments/7/annotated. Answer a pin with annotation_reply." Only this comment shortens:
a pin's text is cut at 300 characters and the comment then says that `review_get` has it whole; what
does not fit in a comment is counted ("And 3 more: see review_get"). The same text with the same
image again (a double click, nothing changed) writes and delivers nothing; a new colour or a small
move changes the image's hash and is sent. A screenshot without live markup, or without a flattened
image yet, is refused.

`review_get` lists each screenshot's pins with coordinates, ids, whole texts and threads and its other
markup, and returns the flattened images as MCP image content after the text, at most 7 MB in one
answer (a line names `attachment_get` for the rest). `attachment_get(id, annotated?)` returns one
screenshot as image content with a text line and its pins, whole. A file over 3.5 MB, or a PNG over
7990 px on a side, is not returned: `review_get` has a text note in its place and `attachment_get`
answers with that message as a tool error. The resource `relaybay://attachments/{attachment_id}/annotated` returns the same
PNG.

A reply reaches the other side through the task's thread as well. `annotation_reply` adds to the pin's
thread, posts an agent reply ("Pin #1 of screenshot #7: …", unread by the owner, in the Inbox) and
marks the owner's unread comments on that screenshot read. An owner's reply in the thread posts an
owner comment that is delivered and wakes the session like any other. Only a live pin takes a reply.

<a id="spec-annotations-viewer"></a>

*Viewer.* A toolbar over the screenshot: Select, Rectangle, Arrow, Pen, Pin, five colours, Undo,
Delete, Hide markup, Send to agent. The markup is an SVG layer in the image's own pixels, in the same
box as the image, so it scales with it. Every shape is saved when it is drawn. Select picks a shape
for Delete (or the `Delete` key); shapes do not move. A click on a pin opens its thread with any tool:
the comment (saved on Save or on leaving the box) and the replies, updated live. Undo (or Ctrl+Z)
takes back this visit's additions and deletions on this screenshot. ←/→ page and Esc closes; while
typing in a pin's boxes the arrows, the zoom keys and Esc stay in the text (Esc closes only the
thread).

*Zoom.* From fit (the default, and on every screenshot opened) up to 8x the natural size, never
below fit (a picture smaller than the screen therefore stays at fit for 1:1). The mouse wheel over
the picture zooms around the pointer (no Ctrl: the viewer is modal), a pinch around its centre;
`+` / `-` step by 1.25 around the middle, `0` fits and `1` is 100 percent; the bar has `-`, the
percentage of the natural size, `+`, Fit and 1:1; a double click (Select tool, or markup hidden)
toggles fit and 100 percent. Zoomed in, the picture pans by dragging with the Select tool or one
finger, by dragging with the space bar held or the middle button under any tool, or with two
fingers while pinching, and never further than its edges; a swipe pages only at fit. One CSS
transform on the box holding the image and the markup layer zooms both, so every shape stays on
its spot; a pointer maps to image fractions from the layer's box on screen. Shapes and lines scale
with the picture (they mark parts of it, as in the flattened image); pins keep their on-screen
size. Every write goes to
the screenshot it was made on, whatever is on screen when it runs. While Send to agent runs, paging,
closing and the tools wait, and the viewer says so. Mouse, pen and
touch work alike; while a drawing tool is on, a swipe draws instead of paging. Agent text is shown as
plain text, never as HTML or markdown. The CSP stays as it is and the viewer's script is a static
file with no third-party library. On the task page a live event no longer reloads the page while
the viewer is open; the page refreshes when the viewer closes.

<a id="spec-session"></a>

**Session and binding.** A session is whoever works on a task: a Claude desktop app session
(`local_<uuid>`, which gets an "Open in desktop" link from `desktop_link_template`), a plain
human-readable name, or a session the hooks created (`kind = 'hook'`). A task is bound to at most one
session at a time; every binding is kept in the task's binding history. A plain name passed to
`task_bind_session` for a task bound to a desktop session does not rebind: it becomes that session's
*alias* and from then on resolves to it. Aliases are global; none is created when a plain session of
that name exists (the call warns and changes nothing). The session identity is for display only; the
token is the only authorisation.

<a id="spec-session-surface"></a>

**Where a session runs** (`session.surface`, migration 0050; [design
0025](design/0025-board-with-the-desktop.md#decided-points)): `desktop` (the Claude desktop app),
`terminal` or unknown. The command hook sends Claude Code's `CLAUDE_CODE_ENTRYPOINT` with
`SessionStart` ([§4](#spec-hooks-surface)): a value starting with `claude-desktop` is `desktop`,
`cli` is `terminal`, anything else (an SDK, an IDE) leaves the surface as it was, and the raw
value is kept in `session.entrypoint`; an older hook client or an http hook sends nothing. A
session with a desktop id (`local_<uuid>`) is a desktop one whatever these say. When the surface
is unknown, the `SessionStart` answer asks the session to say it once with `task_patch(task,
fields={"session_surface": "desktop" | "terminal"}, session=…, hook_session=…)`: the field is
stored on the calling session, not on the task: the one named by `session` (its `hook_session`
linked to it with the guard of any write, so an id another live session linked stays there), else
the session of `hook_session` while no agent has linked that id to itself (otherwise the write is
refused: pass `session` too); neither is a validation error. It writes `session.surface_set` when
it changes; `-`, `''` or null clears it. When an agent links its hook session to a session it
names, the surface, the entrypoint and the start folder the hooks recorded go along unless that
session already has them.

**Start folder** (`session.start_cwd`): the folder of a Claude Code session's first
`SessionStart`, where `claude --resume` finds the conversation again; a `resume`, `compact` or
`clear` start keeps it (the hook's folder may be a subfolder the session moved into), any other
start sets it. `session.cwd` follows every hook for display.

**Resume binds again.** A terminal session's `SessionEnd` unbinds its tasks and marks those
bindings as ended by the session's end (`task_session.unbound_reason = 'session_end'`). A later
`SessionStart` with `source: resume` of the same Claude Code session binds again every open task
whose latest binding is that one (`task.session_bound`, actor the session); a task unbound by hand
or bound to another session since stays as it is.

<a id="spec-run"></a>
<a id="spec-run-limits"></a>
<a id="spec-run-lifecycle"></a>

**Runs and schedules inside the program: removed in 0.12** ([design
0025](design/0025-board-with-the-desktop.md)). 0.9 to 0.11 started the user's own `claude` on a card
(runs, their turns, worktrees and stored lines), had a Terminal tab, a permanent coordinator run,
permission profiles, project run defaults, Settings → Agents and schedules (coordinator turns,
read-only checks and reminders). 0.12 removes all of them, and with them the brake, token budgets,
the Usage page ([§2](#spec-usage)), intake from chats and e-mail ([§12](#spec-intake)) and messages
between sessions ([§1](#spec-session-messages)): a card is worked by a desktop session
bound to it, and the Claude desktop app shows and drives that session. What is left:

- **The upgrade** (migration 0049, data only, [§11](#spec-data)) lists the worktrees runs created and did not remove for the Inbox (`app_setting` `removed_runs_worktrees`); cancels the waiting turns of runs and marks the running ones lost; ends every open run (status reason "runs inside the program were removed in 0.12", ended by `relaybay`); withdraws the open questions that have a run (the coordinator's questions without a card among them) and relaybay's own open questions about a kept worktree, with that reason; expires the open and the approved, unused approvals that have a run; stops the open schedule runs ("schedules were removed in 0.12") and disables every schedule not deleted or done; clears every task's "answer received" mark. Questions and approvals of desktop sessions stay open. Reports that turns wrote stay ordinary reports on their cards; the session rows of
  runs stay as ended sessions in their cards' binding history. The tables of runs and schedules
  were dropped by migrations 0058 and 0059.
- **Worktrees on disk.** relaybay touches nothing on disk (worktrees, Claude Code's transcripts, a
  coordinator folder). The Inbox lists the worktrees runs created and did not remove, with their
  paths, to be removed with `git worktree remove` when not needed ([§2](#spec-board-inbox)).
- **MCP.** Twenty tools and the resource `relaybay://intake/{source}` stayed registered in 0.12 as
  deprecated stubs that did nothing; MCP contract 2 removed them ([§3](#spec-mcp-compat)).
- **Pages.** `/coordinator`, `/schedules`, `/settings/agents`, `/settings/profiles`,
  `/settings/intake`, `/usage`, `/messages` and the run and terminal routes answer 404. Settings has
  the tabs Integrations, Policy, Rules and Notifications.
- **Configuration.** `max_running_turns`, `min_free_memory_mb`, `claude_path`, `usage_enabled` and
  `usage_interval_seconds` in `config.toml` are accepted and ignored; `relaybay doctor` names them under `ignored settings` ([§10](#spec-config)).

<a id="spec-questions"></a>

**Questions to the owner.** An agent's question is an object on its card (`question`,
[§11](#spec-data); [design 0022](design/0022-questions-and-approvals.md#questions)), never a
paragraph of a report. MCP `question_ask(text, default, task, options?, blocking?, default_after?,
kind?, url?, account?, session, hook_session?)`:

- **The caller.** `session` is required: the answer goes back to the session that asked. `task` is
  required; refused on a card in `done`.
- **Every question carries a default** (what the agent does without an answer): one without it is
  refused. **At most 5 open questions per card.** Options: at most 8, each up to 200 characters;
  text up to 2,000; default up to 500.
- **Blocking or not.** `blocking` is false by default: the agent goes on with the default, and the
  question closes with it (`defaulted`, answered by `default`) at `default_after`: a time with its
  zone or a duration (`30m`, `2h`, `1d`), one minute to 30 days ahead, one hour when left out. A
  blocking question takes no `default_after`; the agent ends its turn, and the answer tells it what
  next. The deadline is checked by the server every 15 seconds and on every read of questions (the
  Inbox, the card, `question_list`), so a restart only delays it.
- **"Look at this"** (`kind` `look`), for the owner's visual acceptance: the agent brings up the
  stand, then asks with `url` (http or https, without a user or password) and `account`, the name
  of the test account to sign in with (one line, never `user:password`). It is always blocking
  (`blocking` false is refused), its options are `fine` and `remarks` (others are refused); the
  remarks are the answer text.
- **No secrets.** The text, the default, each option, the URL and the account go through the
  policy's secret check (the detectors and allow patterns of
  [secret rules](#spec-policy)), as an environment's notes do: an account is named, never its
  secret; the refusal names the field and the detector, never the value.
- `question_list(task?, status?)`: newest first, open ones of all cards by default (`answered`,
  `defaulted`, `withdrawn` or `all` too), with the state, options, default, the look URL and
  account, and the answer verbatim. `question_withdraw(question, session, hook_session?)`: only the
  asking session or a coordinator (a session named in `coordinators`, checked like `rules_update`),
  only an open question. **No MCP tool answers a question**: only the owner's button on the board,
  `POST /questions/{id}/answer` (cookie and CSRF).

**The owner's answer.** An option, a text, or both (`<option>: <text>`); for a look question
`fine`, or `remarks` with the remarks as the whole answer (remarks without text are refused). An
open or `defaulted` question takes an answer, an answered or withdrawn one does not. The agent
gets one text: `Owner's answer to question N ("<question>"): <answer>`, the answer verbatim (the
question's text on one line, cut at 300 characters); after a default it gains the line `This changes
course: the question had closed with its default ("<default>") at <time> UTC.` It reaches the
session as an owner comment on the card with that text, delivered through the hooks and the
wake-up poll like any owner comment ([§4](#spec-hooks-comments)); it counts as delivered when the
hooks hand it over (`question.delivered`).

**Waiting.** A blocking question sets the card's `waiting_on` to the owner (the first of
`owner_names`) with `next_step` "Answer question N on the board: …" when it is asked. When the last
blocking question of the card is answered or withdrawn and the card still waits on the owner,
`waiting_on` goes back to the asker and `next_step` says it goes on.

**Events** (with the question's text, as board content like a comment's): `question.asked` (options,
default, deadline, URL and account), `question.answered` (the answer, and `change_of_course` after
a default), `question.defaulted`, `question.delivered` (to which session), `question.withdrawn`;
all on the Activity page and, with the changes, on the Audit page. `question.overdue` (with
`open_minutes`, `blocking` and the default) is written once per question past its deadline
unanswered: a non-blocking one when it closes with its default, a blocking one (it has no deadline)
once it has been open an hour (`question.overdue_at` marks it).

<a id="spec-approvals"></a>

**Approvals.** A push, a merge, a commit or a change on a stand that needs the owner's word is an
object on its card (`approval`, [§11](#spec-data); [design
0022](design/0022-questions-and-approvals.md#approvals)): only the owner's button on the board
decides it, and the guard reads only that row. A coordinator's or another session's message, a
file or a tool result that says "approved" changes nothing.

- **Who is checked.** A desktop session only when the policy of its card's project says
  `approvals.desktop: true` (default false); "No live now" works on desktop sessions as before. A
  session on no card is never checked.
- **What needs one** is policy data, the `approvals` key ([§7](#spec-policy)): `push` (default
  true: `git push` needs an approval that names its branch), `merge` (default true: `gh pr merge`,
  naming the pull request), `commit` (default false: `git commit`, naming its branch), `test`
  (default true: a change on a `test` stand, as the guard's mutating rules see it: a deploy, a
  kubectl or ssh write, an SQL change), `live` (always true: a change on a `live` stand; a layer
  with `live: false` is refused when it is validated, so neither a save nor a proposal can drop
  it), `desktop` (above), `valid_minutes` (default 120, 5 to 1,440: an approved approval not
  used within it expires). `approvals.disable` switches off a built-in action pattern by id
  (`git-push`, `gh-pr-merge`, `git-commit`); `approvals.ignore` lists regular expressions (checked
  like `secrets.allow`): a command segment one of them matches is never an action. Both take `+`
  and `-`. On live neither narrows anything, and neither do the rules `guard.disable` switches
  off: a live change is read with every built-in rule. The approval check does not depend on
  `guard.mode`.
- **The actions and their targets**, read per command segment of a `Bash` call (after `VAR=…`,
  `sudo`, `env` …; a program's folder, `/usr/bin/git` or `C:\Git\cmd\git.exe`, does not matter;
  `2>&1` and other redirections are not commands):
  - `git [-C dir] push [options] [remote [refspec…]]`: each refspec's destination branch
    (`HEAD:feat/x` → `feat/x`), the current branch of the folder (`git rev-parse`, a read) when it
    names none or `HEAD`. `--tags`, `--follow-tags`, `--all`, `--branches` and `--mirror` are
    targets of their own, also next to refspecs. A force push (`-f`, `--force`,
    `--force-with-lease`, `+ref`) and a delete (`--delete`, `-d`, `:ref`) are their own modes: a
    plain push approval allows neither. `--dry-run` and `-n` are not a push.
  - `gh [-R repo] pr merge [<number> | <URL> | <branch>] [-R repo]`: that pull request (its
    number), or the current branch's; `-R` names the repository.
  - A change on a stand: a segment that touches a test or live environment with a mutating rule
    needs a `deploy` approval for that environment (`sql` for a database rule); its git actions
    need their own approvals too. An MCP call (another server's) is one segment.
  - The repository is the one of the project's repositories whose folder holds the call's folder.
    After a segment that moves the shell (`cd`, `pushd`, `Set-Location`), or with `GIT_DIR=`,
    `GIT_WORK_TREE=`, `--git-dir` or `--work-tree`, the current branch and the repository are not
    read: such a push names its branch, and an approval that names a repository does not cover it.
  - Not read: a name the guard cannot read (a `$VAR`, a substitution, a glob, a comment). Commands
    that only mention them (`grep "git push"`, `echo`, `gh pr create --title "… merge …"`,
    `gh pr view`) are not actions. Here-document bodies are data (a commit message, a file),
    except when they feed a shell or an interpreter.
- **Enforcement** ([guard](#spec-guard)): after the level check, a call whose actions need
  approvals is checked against the approved, unused approvals of the cards bound to the session
  (a request opens on the most recently bound). Each action needs its own covering approval: the
  same action and mode, the same branch, pull request or environment, and, when the approval names
  a repository, a call in that repository. Then:
  - **Not covered:** `deny` with "<action> needs the owner's approval: waiting for the owner's
    approval on the board (approval N). End your turn; the decision comes as an owner comment
    through your hooks", and a request opens (`requested_via` `guard`, summary "The agent ran: …")
    unless one is open for that target. At 10 open requests on the card nothing opens and the
    reason says to end the turn and report.
  - **Covered, but with other commands** (another segment, a pipe, a `cd` before it): `deny` with
    "run the approved action alone, as its own command"; the approval is kept.
  - **Covered only by an approval held for another call** (below): `deny` with "approval N (…)
    is held for a call that has not finished yet"; nothing opens.
  - **Covered and alone** (one segment): each approval is **reserved** for this call in one
    transaction (`reserved_for`: the call's `tool_use_id` from its `PreToolUse`, with the call and
    the session), so one approval allows exactly one call: a reserved approval covers no other
    call, and of two calls racing for it only one reserves it (the other is denied, "another call
    holds this approval"). It stays `approved` until the call ran: the call's `PostToolUse` (by its
    `tool_use_id`) marks it **`used`** (when, by which call: the tool and its masked input, and by
    which session; `approval.used`, on the Audit page). A call without a `tool_use_id` uses it at
    once. relaybay gives no permission decision for the call; Claude Code decides as usual.
  - **A held call that never reported back:** a call that got no `PostToolUse` by the session's
    `Stop` or `SessionEnd` counts `used` (relaybay reads no refusals of a desktop session), so an
    approval never lets the same action through twice. A reserved approval expires by
    `valid_until` like any other.
  - **The branch in time:** the check reads the current branch with `git rev-parse` at most
    1.5 s per read and 3 s for all reads of one call, so it answers within the hook client's 5 s
    wait (the wait for a worker thread counts in it); a read is reused for 5 s while the working
    tree's `HEAD` file holds the same bytes (a checkout rewrites it), and is kept only when
    `HEAD` held the same bytes before and after git ran. When git does not answer in time the call is denied with "could not
    tell the branch, run it again" and the approval is not reserved.
  - **On a done card** nothing opens: the call is denied with "<card> is done".
  - A call whose level denies or asks is answered by the level and uses no approval.
  - **On an internal error** of the check the call goes on as with a guard error.
- **MCP.** `approval_request(task, action, target, summary, rollback?, session, hook_session?)`:
  `action` one of `push`, `merge`, `commit`, `deploy`, `sql`; `target` the branch (`feat/x`,
  `backend:feat/x` with a repository of the project, `+feat/x` for a force push, `:feat/x` to
  delete it), the pull request (`12`, `#12`, its URL, or a branch; `backend#12`) or, for a deploy
  or SQL change, the stand in words (`live`, `test`; stored as `<project>/<name>`, matched without
  case; an environment stored before 0.15 with that name is used for the guard); `summary` required
  (up to 2,000 characters: the commits, the files, the CI state); `rollback` (up to 1,000) required
  for a deploy. `session` is required, or the caller's own `hook_session` alone when the board
  knows it (`question_ask` likewise);
  refused on a card in `done` and at 10 open requests per card. The summary, the rollback and the
  target go through the policy's secret check. An open request for the same target (a repository
  only one of them names counts as the same) is filled in (summary, rollback, who asked) instead
  of opening a second. `approval_list(task?, status?)`: newest first, open ones of all cards by
  default (`approved`, `refused`, `used`, `expired` or `all`), with the summary, the rollback, the
  owner's line verbatim and the call that used it. **No MCP tool decides.** A `deploy` request's
  answer adds the card's deploy notes with their state ([§1](#spec-task-deploy)), or a line that the
  card has none; its board item shows them under the rollback.
- **The owner's decision.** `POST /approvals/{id}/decide` with `decision` (`approve` or `refuse`), an
  optional line `text` (up to 1,000 characters, one line) and `from` (`card` or `inbox`), board only
  (cookie and CSRF; a Bearer request is refused, `403`). Only an open request is decided; a deploy
  without its rollback cannot be approved (Approve is disabled; the agent asks again with
  `rollback`). An approval is valid for the project's `approvals.valid_minutes` from the
  decision (`valid_until`); not used by then, it expires (`approval.expired`, checked by the
  server every 15 seconds, by the gate and on reads). The agent gets one line: `The owner approved
  <what> (approval N): one matching call may go through until <time> UTC; run it alone, as its
  own command.` (plus ` The owner's note: <line>`), or
  `The owner refused <what> (approval N): <line, or "no reason given">`, where `<what>` is `push of
  feat/x in backend`, `force push of …`, `push deleting …`, `merge of PR 12`, `commit on feat/x`,
  `deploy on acme-shop/live` or `SQL change on acme-shop/live`. Delivery is the one of
  [questions](#spec-questions): an owner comment on the card, through the hooks. The card goes
  back to the requester when nothing else of it waits on the owner.
- **Waiting.** A request sets the card's `waiting_on` to the owner with `next_step` "Decide
  approval N on the board: …" when it opens.
- **A card that goes to done** takes nothing more from the owner: its open questions are
  withdrawn (by `relaybay`, `question.withdrawn` with the reason), and its open requests and its
  approved, unused approvals expire (`expired`, "the card went to done", `approval.expired`).
- **Events** `approval.requested` (the action, the target, the summary, the rollback, `via`;
  `updated` when an open request was filled in), `approval.decided` (the decision and the line),
  `approval.used` (the call), `approval.expired` (the reason); all on the Activity page and, with
  the changes, on the Audit page. On the board an approval held for a call shows "approved, held
  for a running call".

The check is a seat belt, not access control: an agent with a shell can push another way (a file
it runs, an alias, another tool), and the patterns read text only; live access stays read-only
by level.

<a id="spec-report"></a>

**Report.** Markdown appended to a task, usually at the end of a stage. Its first non-empty line is
the summary (up to 200 characters). A first line `state · size · gist` with a known state
(`inspected`, `changed`, `verified`, `committed`, `pushed`, `blocked`; a second word such as
"verified locally" is allowed) and size (`S`, `M`, `L`) also sets the report's state and size, shown
as badges. Any other first line is checked against the rules ([§6](#spec-rules-modes)).

<a id="spec-report-marks"></a>

**Verified marks.** A claim line may carry `verified: <where>` (`verified: pytest on Windows`,
`(verified: browser at /p/acme-shop)`): the `where` runs to the end of the line or to a closing
bracket, a semicolon or a backtick (a `(…)` inside it stays whole), at most 200 characters. A
mark starts a line or follows a space, `(`, `[`, a backtick or markdown emphasis
(`**Verified:** pytest` counts), so `https://x/verified:abc` and `unverified:` are none; never
after `not `, `no `, `yet ` or `never ` ("Not verified: macOS" is a gap, not a mark); never inside
a fenced code block; an empty `where` or `true`, `false`, `yes`, `no`, `none`, `n/a` is none.
Marks are
parsed when a report is written by `report_add` and stored in
`report_check` (the line, the `where`, the claim line without the mark; at most 100 per report). The
card does not list them again under the report (they are in its text). An M or L report
without any mark gets `Warning (rules): an M or L report has no verified marks: …` in the
`report_add` result, the `report.added` event and under the report on the card; never in `strict`
mode a refusal, and never for an S report or one without a size. The first start with migration
0036 reads the marks of the reports written before it, once, and keeps in `app_setting`
(`report_marks_from`) the first report id after them: older reports show their marks but never
the warning.

<a id="spec-progress"></a>

**Progress, next step, waiting on.** `description` says what to do and why, short; the pseudo-field
`description_add` of `task_patch` (not a column) appends its text as a new paragraph (a blank line,
then the text) to the description read in the same transaction, so two writers keep each other's
lines (the result stays within 20,000 characters, else the write fails); `progress` is the running
summary of everything done so far (rewritten, not appended). `next_step` is what happens next,
`waiting_on` who acts next and nothing else: a name or role, with what happens in `next_step`
(data is never rewritten: a write whose `waiting_on` has more than 4 words gets the answer line
`Hint: waiting_on is who acts next; put what happens into next_step.`, never a refusal or a
`Warning (rules)`). `task_set_stage` and `report_add` take `next_step` and `waiting_on`
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
logged task (a report or a changed `progress` on a card before `live`, or a move back out of
`live` / `done` to an earlier stage) clears the mark in the same transaction with a
`task.tracker_unlogged` event carrying the reason. A report or a rewritten `progress` on a card in
`live` or `done` (through `report_add`, `task_update` or `task_patch` alike) closes it and keeps
the mark; a move back to an earlier stage still clears it. Metadata edits keep the mark; an explicit `tracker_logged` in the same write wins; a new
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
not a feature: it uses the same tools as any session. A session named in `coordinators` in the
config may also use the tools kept for coordinators (`rules_update`, `plan_accept`, `task_archive`,
`task_unarchive`, backlog triage; [§6](#spec-rules-versions)).

<a id="spec-events"></a>

**Event.** Every write appends a row to the event log in the same transaction as the state change.
The event id is monotonic and doubles as the Server-Sent Events id. Kinds: `project.created`,
`project.updated`, `task.created`, `task.updated`, `task.stage_changed`, `task.reordered`, `task.session_bound`,
`task.session_unbound`, `task.tracker_unlogged`, `task.archived`, `task.unarchived` ([§2](#spec-board-archive)), `report.added`, `decision.added`, `session.seen`,
`session.status_changed`, `session.alias_added`, `session.no_live_on`, `session.no_live_off`,
`session.surface_set`, `session.terminal_opened` ([§2](#spec-board-state)),
`comment.added`, `comment.delivered`,
`comment.read`, `plan.acceptance_delivered`, `environment.created`, `environment.updated`, `environment.deleted`,
`environment.access_added`, `environment.access_updated`, `environment.access_removed`,
`environment.proposed`, `environment.proposal_accepted`, `environment.proposal_rejected`,
`live.action`, `document.updated`, `memory.added`, `memory.updated`, `block.updated`,
`project.pulse_set`, the `backlog.*` events ([§12](#spec-backlog)), `inbox.worktrees_seen`
([§2](#spec-board-inbox)),
`question.asked`, `question.answered`, `question.defaulted`,
`question.delivered`, `question.withdrawn`, `question.overdue` (with `question_id` and the text,
[§1](#spec-questions)), `approval.requested`, `approval.decided`, `approval.used` (with
`approval_id`, `action`, `target` and `what`), `approval.expired`
([§1](#spec-approvals))
(`decision.added` carries `supersedes` when it replaces a decision). A stage change that misses a rule carries the warnings in its payload.
Events of runs, the coordinator run, profiles, schedules, the brake, budgets, intake and session
messages written before 0.12 (`run.*`, `coordinator.*`, `profile.*`, `schedule.*`, `brake.*`,
`budget.*`, `intake.*`, `message.*`, `settings.*` of those settings) stay in the log;
nothing writes them now ([§1](#spec-run)).

<a id="spec-comments"></a>

**Comment.** A message on a task's thread: from the owner (written on the board, for the agent of
the bound session) or an agent's reply (`comment_add`, author = the calling session). Markdown, up
to 4000 characters. An owner comment is *sent* when written, *delivered* when a hook of the bound,
exactly linked session handed it to the agent ([§4](#spec-hooks-comments)), *read* when the agent
calls `comment_ack` or replies. An agent reply is unread until the owner marks it read or writes a
comment on the task. Comments of author kind `external` that intake wrote before 0.12 stay in
their threads ([§12](#spec-intake)).

<a id="spec-session-messages"></a>

**Session messages: removed in 0.12** ([design 0025](design/0025-board-with-the-desktop.md)).
Sessions in the desktop app message each other through the desktop app itself. The Messages page and
its sidebar item, the task's Messages block, delivery through hooks, the limits, the notifications
and the Inbox lines are gone; their MCP tools were removed with MCP contract 2
([§3](#spec-mcp-compat)) and the `session_message` table was dropped by migration 0059
([§11](#spec-data)).

<a id="spec-environments-concept"></a>

**Environment.** A project's local, test or live stand with an access map that holds references
only and the agents' permission level there ([§7](#spec-environments)).

Not shipped yet, therefore not concepts of this release: the review checklist, memory notes and
documents (see the [roadmap](ROADMAP.md)).

<a id="spec-board"></a>

## 2. Board

Server-rendered HTML (Jinja2 + htmx, [ADR-0001](adr/0001-htmx-sse.md)), light and dark theme from
the system setting, usable on a phone. A header bar holds the navigation button, the brand link,
breadcrumbs and a live-status indicator (`connecting` / `live` / `offline`). On pages with the
sidebar the brand link is a small home icon (an inline SVG linking to `/`, labelled "Home" for screen
readers, with a title); the window title still names the product. Pages without the sidebar (sign-in)
show the word `relaybay`.

<a id="spec-board-nav"></a>

**Navigation.** Every signed-in page has a left sidebar ([design 0004](design/0004-navigation.md)):
**Inbox** with a live count badge, **All boards**, **All backlog**, **Activity**, **Settings**
(the Integrations page), then **Projects**, each expandable (the current one is open) with **Board**,
**Backlog** (with the count of new items, [§12](#spec-backlog)), **Sessions**, **Docs & rules** and **Settings**, plus the project's
[saved filters](#spec-saved-filters) under Board. The current item is highlighted (`aria-current="page"`). The header button hides or
shows the sidebar on wide screens; the choice is remembered in the browser (`localStorage`, the
sidebar is shown when storage is unavailable). Below 760 px the sidebar is a drawer opened by the
same button and closed by Esc, a click outside or following a link. Items whose feature does not
exist yet are not shown. A project in the archive ([§2](#spec-project-archive)) is not listed.

<a id="spec-switcher"></a>

**Quick switcher.** Ctrl+K (Cmd+K on macOS), or the Search button in the top bar on every signed-in
page (at phone width an icon button), opens a dialog over the page. It searches tasks (key, title,
area), projects (slug, name) and sessions (name, desktop id) through `GET /switcher?q=`, behind the
board cookie (without it: 401), and shows at most 20 results as `{kind, title, detail, url, key,
area, area_color}` (a task's area shows as its chip), written into the page as text. Matching ignores case and every word typed has to match one of the item's
texts: the whole text, its start, the start of a word, anywhere, then the letters in order with the
fewest gaps; equal scores keep the most recently changed task first. It looks at the 2000 tasks and
500 sessions changed last. With nothing typed it lists at most 15 recent tasks and every project, no
sessions.
↑ / ↓ move, Enter or a tap opens: a task's dialog on its board (`/p/<slug>?task=<key>`), a board, or
a session's row on the Sessions page (`/sessions#session-<id>`, highlighted). Esc or a click outside
closes it. With focus in an input, text area, select or editable text the shortcut does nothing and
the browser keeps it; elsewhere the browser's own Ctrl+K is prevented. A failed search says so in the
dialog.

<a id="spec-saved-filters"></a>

**Board filters and saved filters.** The board takes `stage`, `area` (an area's name, ignoring
case; the select "Any area" next to the stage filter lists the project's areas and shows only when it
has some), `waiting_on` (ignoring case), `session` (name, alias or desktop id) and `q` (key or title)
in its URL, together with `archived` and `page`;
the form above the columns sets them, and `Clear` drops them. A value that cannot be used (an unknown
stage, a malformed desktop id) shows the unfiltered board with the reason in an error line. A stage filter leaves the other columns
empty and opens the page and phone tab of that stage. Live column refetches carry the same query, so
the board keeps showing only what matches; while a filter is on, a changed task refetches its column
rather than its card, so it leaves or enters the view at once. With a filter on, the board offers
**Save filter**: a name (at most 60 characters, unique per project) stored in `saved_filter`. Saved
filters are chips above the columns, each with a delete button (a browser confirmation), and items
under Board in the sidebar on every page; opening one is an ordinary board URL, so it can be shared on
the same machine. `archived` and `page` are not part of a saved filter. Writes are cookie + CSRF form
posts; events `filter.saved`, `filter.deleted`.

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
| Audit | `/audit` | Who did what on which live stand, with filters ([below](#spec-board-audit)). |
| Project settings | `/p/<slug>/settings` | The project's repositories ([below](#spec-board-repos)), the archive ([below](#spec-project-archive)) and areas ([below](#spec-board-areas)); the old `/p/<slug>/environments` redirects here (`301`, [§7](#spec-environments-page)). |
| Docs & rules | `/p/<slug>/docs`, `/p/<slug>/docs/<doc>`, `/p/<slug>/docs/<doc>/edit[?section=]`, `/p/<slug>/docs/<doc>/diff?from=&to=` | Documents, decisions, memory notes, blocks, the effective rules and search ([§8](#spec-memory-page)); editing a document ([§8](#spec-documents-edit)). |
| Integrations | `/settings/integrations` | Claude Code on this machine ([§5](#spec-setup)); `/settings` redirects here. |
| Backlog | `/p/<slug>/backlog`, `/backlog` | Backlog items of a project or of all projects ([§12](#spec-backlog)). |
| Notifications | `/settings/notifications` | Browser notifications: state and the button that asks for the permission ([below](#spec-board-notify)). |
| Screenshot | `/files/<id>` | One stored image behind the board cookie ([§9](#spec-security)). |
| Annotated screenshot | `/files/<id>/annotated` | The latest flattened PNG with the owner's markup ([Annotations](#spec-annotations-flatten)). |
| Policy | `/settings/policy` | The effective policy per scope with the source of each value, preset, layer editors and agent proposals ([§7](#spec-policy-page)). |
| Rules | `/settings/rules` | The rules per scope, version history with diffs, proposals, roll back ([§6](#spec-rules-page)). |
| Sign-in | `/login` | Token form; also accepts `?token=…` once and sets the cookie. |

<a id="spec-board-paging"></a>

**Paged board.** Six columns do not fit one screen: on wide screens page 1 shows plan, dev,
review and test, page 2 live and done. The pager next to the search (links "Plan · Dev · Review ·
Test →" and "← Live · Done") and the keyboard ← / → switch pages; the keys are ignored inside
inputs, text areas, selects and editable text, with a modifier key, and while a dialog or the
screenshot viewer is open. The page is in the URL (`?page=2`, kept with search and archive
filters; out of range clamps), switching does not reload, and live updates keep refreshing every
column. Below 760 px there is no pager: the stage tab bar has six tabs. With "Show archived"
the Archive column ([below](#spec-board-done)) joins page 2 ("← Live · Done · Archive") and the
tab bar gets a seventh tab, **Archive**.

<a id="spec-board-cards"></a>

**Cards** show only the essentials: key, the area as a coloured chip ([§1](#spec-areas); the
palette has a light and a dark pair per colour), tracker badge (`<tracker_name> <tracker_ref>`, a link when
a URL is set, ✓ when logged, "not logged" with `require_tracker_log`) and an icon-only "open in
desktop" link with the session's hook status dot in the top row, and the word of the card's
state line ([below](#spec-board-state)) next to it (a dot of its own when there is no desktop link;
nothing for No session; its title lists why it waits for you); then title, waiting on (cut at the
first colon, the full text in the chip's title; the task dialog shows it in full), branch and
the next step (two lines, muted with "stale" when stale). Done cards hide waiting on and the next
step. A card waiting for another one shows a `blocked by <key> · <stage>` chip per blocker that
still blocks ([§1](#spec-task-blockers)), a link that opens that card (its dialog when it is on the
same board). A task with a checklist shows **Review 2/5** (accepted / all; green when all are accepted,
red-tinted while one is rejected), which opens the dialog on its Review tab. A task with a plan
shows **Plan 2/5** (done / all) with `· draft` while it is not accepted, the number of deviations
highlighted and `· now: <step in progress>` (one line, cut with an ellipsis), which opens the
dialog on its Plan tab.

<a id="spec-board-questions"></a>

**Questions fold.** The task dialog and the task page show a **Questions** fold loaded from
`/p/<slug>/t/<key>/questions` and refetched on the card's `question.*` events (not while an answer
is being typed): the card's open questions, the oldest first, as in the Inbox
([below](#spec-board-inbox)), then the closed ones folded (answered with when it was delivered, or
"waits as owner comment #N"; closed with the default; withdrawn by whom, with the reason). A question closed
with its default still takes an answer, which the agent gets as a change of course. The answer is
`POST /questions/{id}/answer` with `choice` and/or `text` and `from` (`card` or `inbox`, the panel
an htmx post swaps; a plain form post is redirected back with a flash), board only (cookie and
CSRF; a Bearer request is refused, `403`).

<a id="spec-board-approvals"></a>

**Approvals fold.** Below Questions, an **Approvals** fold loaded from
`/p/<slug>/t/<key>/approvals` and refetched on the card's `approval.*` events (not while a line is
being typed): the card's open requests, the oldest first, as in the Inbox, then the decided ones
folded: refused, expired (the card went to done), approved "not used yet", or used with when
and by which call (`Bash: git push
origin feat/x`), each with when it was delivered ("waits as owner comment #N").
Approve and Refuse post `/approvals/{id}/decide` ([§1](#spec-approvals)).

<a id="spec-board-repos"></a>

**Repositories block.** The project's Settings page starts with
**Repositories**: one row per repository in order with its name, kind, path and **Open in VS
Code**, **Up** / **Down** buttons, an **Edit** fold (name, kind, path) with **Remove** (asks first:
tasks lose their link, their pull requests stay) and **Add a repository**. Writes are plain form
posts to `/p/<slug>/repos`, `/p/<slug>/repos/<id>` and `/p/<slug>/repos/<id>/up|down|delete` with
the session cookie and the CSRF token; a Bearer request is refused (`403`).
A refused value (a name taken, an unknown kind, no path) comes back next to its form. Each change
is a `project.updated` event with `changes.repos` (`added`, `changed`, `removed` names) and
`repo_path` when the first repository's path changed.

<a id="spec-board-areas"></a>

**Areas block.** After Repositories the page has **Areas** ([§1](#spec-areas)): one row per area in
order with its chip, how many cards carry it (a link to the board filtered by it), a name field and a
colour select with **Save** (a rename, a recolour, or a merge into the area of that name), **Up** /
**Down**, **Delete** (asks first, naming how many cards lose it) and a field to add one. Writes are
form posts to `/p/<slug>/areas`, `/p/<slug>/areas/<id>` and `/p/<slug>/areas/<id>/up|down|delete`
with the session cookie and the CSRF token (Bearer: `403`); a refused value (no name, over 32
characters, an unknown colour, the limit of 24) comes back next to the block. Each change is a
`project.updated` event with `changes.areas` (`added`, `changed`, `moved`, `merged`, `removed`); a
rename or recolour also sends `task.updated` for each card that carries the area, so open boards
refresh their chips.

The task form has an **Area** field next to Branch with the project's areas as suggestions; the
task dialog shows the chip after the key in its head, and the task view (dialog and task page) an
**Area** row. Inbox card rows show the chip after the key.

<a id="spec-board-code"></a>

**Code row.** A task whose project has repositories, or that has pull requests, shows a Code row on
its card and in the task dialog / page ([§1](#spec-task-code)): one line per linked repository with
its name, its kind, its branch (the one the hooks saw checked out in that repository while a
bound session worked there, stored on the link, migration 0057; until then the task's own branch;
the card drops its separate branch chip once a repository is linked), its worktree when one is
set (the folder, with its own **Open in VS Code** icon), its
pull requests (`#123` when the number is known, else `PR`; each opens in a new tab) and an
**Open in VS Code** icon (`</>`, the path and the words on hover), a `vscode://file/<path>` link
(backslashes become `/`, a Windows drive stays `C:`, other unsafe characters are percent-encoded; a
leading `~` is left to the editor). With no repository linked there is no repository line (until
0.15 a folded **Repos: all**); `task_get` then names the project's repositories and asks the
session to link the ones it touches. Pull requests tied to no listed repository follow as
**PRs:**, each named by the repository in its link (`mobile #7`). The compact card
([below](#spec-board-compact)) shows only a count instead: `Code: 2 repos, 3 PRs` (nothing when
the task has neither). A `task.updated` event refreshes the card.

<a id="spec-board-compact"></a>

**Compact** (a toggle in the board's tools row, on phones too) leaves key, tracker badge, title and
the waiting-on chip on every task card; the "open in desktop" icon, the Review and Plan badges, the branch and the next step are
hidden, and the Code row turns into its count. A chevron button on a card
(`aria-expanded`, shown only in this mode) expands just that card; a click on the card still opens
the dialog. Both choices are kept per browser in `localStorage` (the expanded cards by task key, the
last 200), survive live refreshes of a card or a column, and live nowhere on the server. Without
storage they last until the page is left; in the desktop program's window (private mode) they last
until the program quits.

<a id="spec-board-order"></a>

**Card order.** Each stage column shows its cards in the owner's order, `task.rank` ascending (ties:
the most recently updated first). A new task, and a task that changes stage any other way than by a
drop (the edit form, MCP, an import, reopened plan work moving it back to dev), goes to the top of its column; other edits keep its place.
Ranks are whole numbers: a drop renumbers the whole column 1..n (archived done tasks included), so
they stay small and never need a value between two others; a task put on top takes the column's
lowest rank minus one. The owner reorders on the board only (cookie + CSRF, `POST
/p/<slug>/t/<key>/move` with `stage`, `before` and `after` task keys; a Bearer client gets 403); no
MCP tool sets a rank, and `task_list` and the digests keep their order (`priority` first, then the
most recently updated). On the board the owner's order is the priority: a task's `priority` no
longer sorts its cards.

- **Drag** a card (mouse or pen: press and move; touch: hold for about a third of a second, then
  move) within its column or into another column of the page shown; an accent line marks where it
  lands, and near the window's top or bottom and the board's left or right edge the page scrolls.
  A release under a short column counts as its end; Esc or a release outside the columns cancels. The card is placed right before the card below the
  line, else right after the one above it, else on top.
- **Card menu** (the ⋯ button on every card, in compact mode too; Tab, Enter, Esc): **Move up**,
  **Move down** (disabled at the ends) and **Move to <stage>** for each other stage, which puts the
  card on top of that column. The keyboard focus stays on the card's menu button after the move and
  after the live refresh.
- A drop into another column is a stage change like the edit form's (rules, `strict` refusal, done
  sets the closed time, `task.stage_changed` with its warnings, actor `board`), in one transaction
  with the placing. Rule warnings show above the columns; a refusal (a rule in `strict` mode, a
  neighbour that left the column meanwhile: `<key> is no longer in this column: the board changed
  meanwhile. Try again.`) puts the card back and shows `Not moved: <reason>`. The notice stays until
  the owner closes it (the × button, `aria-label="Dismiss this notice"`) or the next move replaces
  it; live refreshes do not bring it back. When the plan of the moved task is accepted
  (`plan.accepted`), its "needs an accepted plan" line goes away; other lines stay.
- A changed order writes one `task.reordered` (`key`, `stage`, `position` from 1), which refreshes
  the column on every open board; a drop on the same place writes nothing. The rank is not an edit:
  `version` and the updated time stay, so an open edit form still saves. The event is left out of
  the digests' changes and makes no tray or browser notification.

<a id="spec-board-merge-order"></a>

**Merge order fold.** With two or more cards in `test`, the test column has a closed fold
**Merge order** under its header: the numbered cards (each key a link to its dialog) and the
warnings of [§1](#spec-merge-order). It lists every test card of the project whatever the board's
filter, since the order belongs to the project, and refreshes with the column (a stage change, a
reorder) (code: web/templates/_column.html).

<a id="spec-board-state"></a>

**The session card** ([design 0025](design/0025-board-with-the-desktop.md#what-gets-stronger)).
The task dialog's Overview and the task page put what the owner acts on first. An open card (not
done, not archived) shows, in this order: the title and the **state line**; what waits for the
owner: **Approvals** and **Questions** with an open item, **Unread replies** (the agent replies not
marked read, as the Inbox counts them); **Next** (cancelled mark, next step, waiting on, Blocked by,
Blocks); **Plan** (done/all and each step with its status; the Plan tab keeps the full plan and
Accept plan); the **Last report** (older ones inside it); **Deploy notes**; **Description** and
**Done so far**; then, folded: **Task details** (area, stage, branch, code, tracker, document,
sections, archive, backlog, updated), **Session and its history**, Approvals and Questions holding
only decided or closed items, Decisions, Findings, Document, Tracker history and Actions on live. A
done or archived card leads with its summary, the Last report and **Code** (the Code row: branches,
PRs), open; what waits for the owner still comes first when there is any, Next stays open only
for a cancelled card, everything else is folded (code: web/templates/_task_view.html).

A block with nothing in it is left out, never shown with an empty-state sentence: no report, no
approval or question at all, no unread reply, no next step or link, no plan steps, no deploy notes,
no description, no progress, no decisions, findings, tracker history or actions on live. Session and
its history stays while it can bind a session (the picker), even with nothing bound. A card without
comments folds the comment box into **Add comment**, the form inside. The view renders its questions
and approvals itself; a live event that may fill a block left out (a question, an approval, a
comment, a plan change) refetches the whole view, held while an answer is being typed (code:
web/static/app.js:queueView). The comment box comes last, outside every fragment that live events
refetch, so an event never clears a half-typed comment (the dialog's Edit task fold follows it).

The state line is derived and never set by hand (`relaybay.domain.card.card_state`), in a large
word with the session's dot:

- **Waits for you** when the card has an open question or approval request, is in `review` or
  `test` with a checklist, has `waiting_on` naming the owner (`owner_names`; not on a done card) or
  its bound session asks for its user (hook status `waiting`, with the notification's text,
  [§4](#spec-hooks-events)); each reason is listed under the word;
- else **Working** or **Idle** by the bound session's hook status (a bound session without one
  counts as idle), **Ended** when the session of the latest binding has ended (a terminal
  session's end unbinds it; a desktop session stays bound), **No session** otherwise.

Under the word: the session's name and last activity with its age. The line refreshes live on the
card's events and on `session.status_changed` of its session (not while an answer is being typed).
**Open** is a menu with two items whenever the card has a session (the bound one, or the ended one
of the latest binding); a disabled item stays in the menu, greyed, with its reason in one line:

- **In desktop** opens the session's `desktop_link_template` link, for a desktop session
  ([§1](#spec-session-surface)) live or ended. Disabled for a terminal session ("started in a
  terminal: the desktop app cannot open it") and for a desktop session without a known desktop id
  ("its desktop id is not known yet"). A session of unknown surface follows its desktop link when it
  has one. A live desktop session whose last start came from a terminal (resumed there with In
  terminal) gets both items disabled with "running in a terminal".
- **In terminal** posts to `/sessions/<id>/open-terminal`, which opens a new terminal window on this
  machine running `claude --resume <its Claude Code id>` in its start folder. Enabled for any ended
  session, desktop, terminal or unknown, with a resumable Claude Code id and a known folder.
  Disabled for any live session ("running in the desktop app", "running in a terminal", "still
  running": a second process on a live session is unsafe), without a Claude Code id ("no Claude
  Code session id to resume"), without a folder ("its folder is not known"), and while a terminal
  opened for it has not started yet. The endpoint refuses a live session with the same reason, and
  a refusal (409) is shown in the state line.

The terminal endpoint takes only the session's internal id, from a signed-in browser (cookie and
CSRF; refused with a Bearer token) on the machine itself: the client address and the request's
`Host` must both be loopback (`localhost`, `127.0.0.1`, `[::1]`), else 409. So the board on a
phone, and a board published through a reverse proxy (whose clients come from 127.0.0.1 but ask
for the published name), never open a terminal. With a published name (`external_url`,
`allowed_hosts`) the same holds on the machine itself, because a tunnel running there forwards a
phone's request from 127.0.0.1 with that name: the 409 says to open the board at
`http://127.0.0.1:<port>` on this machine. Opens are serialized: a second Open is refused until
the opened terminal's `SessionStart` arrives (or a short hold runs out), and the session is read and
checked under the same lock as the start, so a click queued behind another sees the session already
running and cannot start a second process. The command is fixed, the id must be a lowercase
UUID read from the database, the start folder must exist, and every command is an argument list:
Windows Terminal (`wt -d <folder> claude --resume <id>`, unless the folder holds a `;`), else
`claude` in a console window of its own; on macOS `osascript` runs a fixed script that gets the
folder, `claude` and the id as arguments and quotes them for Terminal's login shell (a folder or a
`claude` path with a backslash or a single quote is refused, since a non-POSIX shell such as fish
reads them otherwise); elsewhere the first of `x-terminal-emulator`, `gnome-terminal`, `konsole`,
`xterm` on the PATH. The terminal's environment is the server's without `CLAUDECODE` and the
`CLAUDE_CODE_*` variables. No terminal or no `claude` on the PATH: 409 with that reason.

One Open at a time per session: after an Open, another one is refused (409, "has not started
yet") until that session's next `SessionStart` arrives or 60 s pass, and the check and the start
run under one lock; meanwhile **In terminal** stays disabled with "a terminal is opening". Each opening
writes `session.terminal_opened` (session, folder, program, the keys of its cards; the card
refreshes on it; on the Audit page with "Changes"). relaybay never starts a session otherwise.

While no session works on the card (No session, Ended, or Waits for you without a live bound
session) the line has **Copy the brief**: the text a new session needs (the card's key, project
and title; the line that tells it to bind itself with `task_bind_session` and pass its
`hook_session`; the task's digest as `task_context` builds it without a reader: next step,
waiting on, branch, the description (whole, in a section of its own when the digest cuts it), done
so far, the plan and its current step, decisions, linked document sections, environments, rules;
the card's attachments with their paths on disk; the report line). It goes to the clipboard, or,
where the clipboard API is not available, the fold under the line opens with the text selected.
relaybay starts nothing.

<a id="spec-board-dialog"></a>

**Task dialog / page** shows every field (stage with note, waiting on, next step, branch, tracker,
document path, updated time and version), description and progress in full, the latest reports
with state and size badges: the newest in full, older ones as an accordion whose summary is the
report's first line with its badges, author and time, expanding to the full text (a live refresh
or a reload of the task page keeps the opened ones, by report id, for this browser tab) (document links in the texts point at their sections,
[§8](#spec-documents-links)), the linked document's panel, decisions, the bound session with its hook status and last activity,
"Open in desktop" and "Copy link", the session binding history, the tracker history, an
**Unbind session** button (with confirmation), a session picker (**Bind a session** menu, one click binds; title first, kind, id and status small: the project's sessions
first, then the others, most recently seen first; the same binding history and
`task.session_bound` event as `task_bind_session`, actor `board`), the comments and the edit form. A stage move that
misses a rule shows the warning in the form (or the refusal in `strict` mode).

<a id="spec-board-folds"></a>

**Collapsible blocks.** Every block of the Overview tab and of the task page except the state line
and the comments (and the Add comment fold of a card without comments) folds under its title: a
chevron, the whole title row clickable, by keyboard too. They start open or folded by the order
above. The owner's choice is kept per browser
in `localStorage` by section, not by task (collapse Decisions once and it stays collapsed on every
task); a block that starts open on an open card and folded on a closed one keeps the two choices
apart. It survives live refreshes and lives nowhere on the server; without storage it lasts until the
page is left, in the desktop program's window until the program quits. A collapsed block keeps its
content, so a half-written comment or an edit is not lost. Plan and Review are not folded.

<a id="spec-plan-board"></a>

**Plan tab** (task dialog, `?tab=plan`; a Plan section on the task page; tabs Overview | Plan |
Review): the plan state (**Accepted** with who and when, or **Draft** with an **Accept plan**
button: board-only, cookie and CSRF, refused with a Bearer token; actor `owner`), "2 of 5 done ·
1 deviation", then the steps with their status, "what and why" and checkpoint (conformance,
outcome, link, time); a deviation is highlighted. Under the current step (in progress, else the
first one not done): `Now doing: <activeForm of the todo item in progress>` and the agent's todo
collapsed. A step added after acceptance shows "Added after acceptance by <who> <when>: <reason>".
Without plan steps: the todo marked **Agent's plan, not accepted**, or a note on how
the agent writes a plan. **History (n)** lists every revision, newest first, with its reason,
author and time and the titles it changed (`- 2. old` / `+ 2. new`, a step added after acceptance
`+ 4. title (added after acceptance)`). The card's Plan badge adds `· N h` to `draft` when the
draft hint applies ([§1](#spec-plan-draft-hint)). The tab refreshes live on
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
Esc or Close to leave (the dialog stays open), swipe left or right on phones; zoom with the wheel,
a pinch, `+` / `-` / `0` / `1` or the bar ([details](#spec-annotations-viewer)).

<a id="spec-board-notify"></a>

**Browser notification.** With the permission granted, every open board tab shows "Waiting for
your acceptance: <task title>" when a `task.stage_changed` event moves a task to `review` or
`test` (one per task: the task key is the notification tag); a click opens its Review tab. The
permission is requested only by the **Allow notifications** button in Settings → Notifications,
never on page load; the page shows whether this browser allows, blocks or has not decided. The
desktop program has its own [tray notifications](#spec-desktop-notify), with no tab needed; with
both on, a review is told twice.

<a id="spec-board-comments"></a>

**Comments** (task dialog and task page): the thread oldest first, then a comment box (markdown, up
to 4000 characters, CSRF-protected form). Each owner comment shows `sent <age>`, then
`delivered <age> to <session>` or `not delivered yet` with its reason (no session bound; the session
ended; the session has not linked its hooks yet, i.e. passed no `hook_session`; the session waits
for its user, so the comment arrives with its next tool call; the session is idle and the wake-up
hook is not waiting, [§4](#spec-hooks-wake)), and `read <age>` once acknowledged (exact UTC
times on hover); agent replies show their author, age and `unread` until marked read. **Mark replies
read** marks the task's unread replies read; sending a comment does the same. A note under the
thread says which session a new comment goes to, or why it would wait. The thread
refreshes live on `comment.*` events of its task; a draft in the box is kept (on the task page, other
changes then refresh the details instead of reloading the page).

<a id="spec-board-done"></a>

**Done column and archive.** The done column shows tasks closed within `done_window_days` (default
7), capped at 200 with a note. Older done tasks are archived on the board only: "Show archived"
(`?archived=1`) and the search box (`?q=`, key or title, also searches the archive) bring them back.
"Show archived" adds an **Archive** column after done (`id="col-archive"`, refetched live from
`GET /p/<project>/col/archive`, which is not a stage): it lists every archived task of the project
(by hand, from live or by age), newest archived first (the hand mark's time, or the end of the done
window for one archived by age), capped at 200 with a note, with the board's filters (search, stage,
area, waiting on, session) applied; done then shows only the tasks not in the archive. Without
"Show archived" there is no Archive column, and a search shows its archived hits in done. The
Archive column is no drop target and its cards cannot be dragged or moved from the card menu: a
card leaves it by **Back from the archive**. `task.archived` and `task.unarchived` (and any other
task event touching done) refetch the done and the Archive columns.
With `require_tracker_log = true` a done task without the tracker mark is never archived by age,
in a project that keeps a tracker (any of its cards has or ever had a tracker reference, or has a tracker link now);
in other projects done cards leave by age and show no "not logged" badge.

<a id="spec-board-archive"></a>

**Archive by hand.** A done card's menu and its task view have **Archive**: the card leaves the done
column at once (`task.archived`, the done column refetched live) and stays findable with "Show
archived" and in search. The mark (`task.archived_at`, `archived_by`) archives the task whatever
`require_tracker_log` says; an unlogged task archived by hand stays in the Inbox work-log line (in a project that keeps a tracker)
until its work log is marked. On an archived card (by hand or by age) the same places offer **Back
from the archive** (`task.unarchived`, with `archived_by` of the hand mark, none when the window had
archived it): the hand mark is cleared and `task.unarchived_at` is set, and the done window counts
from the later of `closed_at` and `unarchived_at`, so the card shows in done for another full
window. Only a task in `done` is archived or brought back (otherwise `Only a done task goes to the
archive; <key> is in <stage>. Nothing changed.`); a repeat changes nothing and records nothing; any
stage change clears all three columns, so a task moved back to done starts fresh. Nothing is
deleted at once (after `archive_purge_days` it is: [Deleting from the
archive](#spec-archive-purge)), and the marks do not bump the task's version (an open edit form does not conflict). Board
writes are cookie + CSRF only (`POST /p/<project>/t/<key>/archive` and `/unarchive`; a Bearer client
gets 403). Over MCP, `task_archive` / `task_unarchive` are for coordinator sessions only; anyone
else is refused with `<tool> is only for the coordinator sessions named in the server's config; …
Nothing changed. The owner archives done cards on the board.` Both events are on Activity and, with
"changes", on the Audit page.

<a id="spec-board-archive-live"></a>

**A live card in the archive.** A card in `live` has **Archive** too (card menu and task view, one
confirmation): it is closed and archived in one step, the owner's own act, so no rule is checked
(no warning, no refusal in strict mode). The move is an ordinary `task.stage_changed` live → done
by the same actor, then `task.archived` with `from: live`; `task.archived_from` keeps `live`.
**Back from the archive** on such a card returns it to `live` (`task.stage_changed` done → live,
then `task.unarchived` with `to: live`), which clears every archive mark and keeps the work-log
mark (`tracker_logged`) as it was, so the way back unlogs nothing. A card moved by hand after
being archived from live forgets where it came from: its next archive is an ordinary done one.
Nothing is closed for good: a live card with an open question, an open approval request or an
approved, unused approval is not archived (a move to done would withdraw or expire them) and the
refusal says `<key> has an open question, an open approval request or an approved, unused
approval: answer, withdraw or use it first, the archive closes nothing for good. Nothing
changed.`; the confirmation says so too. `task_archive` / `task_unarchive` do the same for
coordinators. Only `done` and `live` cards are archived; any other stage is refused with `Only a
done or live task goes to the archive; <key> is in <stage>. Nothing changed.`

<a id="spec-board-archive-bulk"></a>

**Archive all shown.** Under the cards of the done column, **Archive all shown (n)** (n counts the
cards on screen not already in the archive, so it follows the board's filters) asks once and
archives exactly those cards (`POST /p/<project>/archive`, form field `keys`, the keys separated by
spaces, at most 500; cookie + CSRF; JSON `{batch, keys, message}`). Only done cards are archived in
bulk: a card that left done meanwhile, is already archived or no longer exists is skipped; a key of
another project refuses the whole write. A live card is archived one at a time. All of them go in one transaction under one batch id, carried by each
`task.archived` as `batch`. The board's notice offers **Undo** (`POST /p/<project>/archive/undo`,
form field `batch`; JSON `{keys, message}`), which brings back exactly the cards of that batch that
are still archived from it, each `task.unarchived` with the same `batch`; a
card brought back or archived again since is left as it is.

<a id="spec-project-archive"></a>

**A project in the archive.** The project's Settings page has **Archive project** (one
confirmation) and, while it is archived, **Back from the archive** (`POST
/p/<project>/project/archive` and `/unarchive`, cookie + CSRF); over MCP,
`project_upsert(slug, archived=true|false)`, for coordinator sessions only like `task_archive`
(anyone else: `project_upsert(archived) is only for the coordinator sessions named in the server's
config; … Nothing changed. The owner archives a project on its Settings page.`; the other
fields of `project_upsert` stay open to every session). Both write `project.updated` with
`changes: {"archived": true|false}`; a repeat changes nothing and records nothing. `project.archived_at`
holds when. An archived project leaves the sidebar, All boards and `project_list`; the Inbox (its
cards, review checklists and new backlog items); `portfolio_digest` (its project line, waiting and
stale cards, decisions and the changes since the last read). Archiving deletes nothing: its board, cards
and history stay, the board says it is in the archive, `project_digest` and `task_*` tools still
read it, and All boards links to **Archived projects (n)** (`/?archived=1`), which lists them with
the way back. The hooks do not place a session in an archived project by its folder.

<a id="spec-archive-purge"></a>

**Deleting from the archive.** Two deletions are for good; before each one relaybay writes an export
of what it deletes to `deleted/` in the data directory, in the same transaction as the deletion (a
failed write deletes nothing): `<UTC time>-<project>[-<task key>].json` with the database rows by
table (`tables`, every row the deletion takes along through the foreign keys, and the events), or
a `.zip` of that JSON (`export.json`) and the attachment files when there are attachments
(`files`; one gone from disk is listed in `missing_files`). The attachment files are copied into
the zip before the write transaction; only the JSON is written while it holds the write lock (a
file added in between is copied then). A transaction that rolls back removes its export again, and
a failed export refuses the deletion with `Could not write the export to <folder>: <reason>.
Nothing was deleted.` Nothing deletes the exports. Besides the foreign keys, a deletion also removes
the rows keyed by the deleted ids that have none, since ids are reused: the search index rows of
the deleted items, the digest read cursors, `import_item` records of `relaybay import` (in the
export) and, for a project, the sessions' `rule_seen` marks (in the export).

- *A task archived long ago.* A task in `done` with the archive mark (`archived_at`, by hand or from
  live) older than `archive_purge_days` (default 30; 0 never), and not brought back since
  (`unarchived_at` empty or before `archived_at`), is deleted by relaybay at the start and then
  once a day, each task in its own transaction (one that fails is logged and tried again at the
  next pass; the others go on). It takes with it everything that hangs on the task: reports (and
  their `verified:` marks), comments, review steps, the plan and its revisions, attachments (the
  files too) with their annotations, approvals, questions, PR and repository links, its blocker
  links in both directions (`task_block`), bindings,
  tracker history, intake threads, schedules of the task (active ones too), its token budgets and
  brakes, its runs with their turns and events, its usage per day (`usage_task_day`: the usage
  history of the project shrinks by that task's share) and its task decisions (scope `task`). A
  task decision that superseded a decision outside the task stays, as a project decision without
  the task, so that topic keeps a current decision. A done card the done window archived by age
  has no mark and is never deleted. `require_tracker_log` does not hold a card back: one archived
  by hand without its work log is deleted all the same, and its data stays in the export
  (decided by default). What stays: backlog items and intake messages it was linked to (the link
  is cleared), messages between sessions, and its events (their `task_id` is cleared; the payload
  still names the key). The event `task.purged` (actor `relaybay`, on Activity and, with
  "changes", on the Audit page) names the key, title, archive mark, the export (`deleted/<file>`)
  and the row count per table. Keys are never given out again, so a read of a key that no longer
  exists (`task_get`, `task_context` and every other tool that takes a card) looks for that event
  and answers `Task <key> was deleted from the archive on <date>; its export: deleted/<file>.`
  (`export file not recorded` when the event names none) instead of `not found`.
- *An archived project, on request.* The Settings page (`/p/<project>/settings`) of an archived project has, next to Archive, **Delete
  project**: the slug typed into the form and one confirmation (`POST /p/<project>/project/delete`,
  form field `confirm`, cookie + CSRF; a wrong slug, a project not in the archive or an export
  that could not be written shows the reason with status 400; a repeat after the deletion goes to
  All boards with `There is no project <slug> (any more).`). Over MCP, `project_delete(slug, confirm=slug)` is for coordinator sessions only
  (`project_delete is only for the coordinator sessions named in the server's config; … Nothing
  changed.`). A project not in the archive is always refused (`<slug> is not in the archive: only an
  archived project is deleted. …`), and so is a `confirm` other than the slug (`Deleting <slug>
  cannot be undone: confirm with the project's slug, <slug>. Nothing changed.`). The project goes
  with its tasks (as above), documents (the rows and their index only: a file document's file on
  disk is never touched), backlog, decisions, notes and blocks, rules, policy layers, stands, areas,
  repositories, filters, schedules and its events; sessions placed in it lose the link. It is gone
  from `project_list`, the board, All boards, the Inbox, the digests, `portfolio_digest`, search and
  Activity. The event `project.deleted` (no project id, the actor who asked) names the slug, name,
  task count, export and row counts; it is on Activity and, with "changes", on the Audit page.
- *A project document, on request:* the same export, named `<UTC time>-<project>-doc-<name>.json`
  ([§8](#spec-documents-delete)).

<a id="spec-board-inbox"></a>

**Inbox** ("Needs you") lists, grouped, with a count per group:

- **Questions:** the open questions of all cards, the oldest first ([§1](#spec-questions)): the
  card, who asks, "blocking" or "goes on with the default at <time> UTC", its age, the text, a look question's URL as a link that opens in the
  owner's own browser (`target="_blank"`, which the desktop program hands to the default browser)
  and the test account's name, the options and default; a button per option and a text field
  ("Send the text" for a free answer). An answer swaps the Inbox; a refusal shows its reason there.
  Each question counts as one row; a card waiting on the owner for its open question is not
  counted again under Cards. While an answer is being typed (in the Inbox, the task
  page or the task dialog), live refreshes that would swap it away wait until it is sent or
  emptied, then run once.
- **Approvals:** the open approval requests of all cards, the oldest first
  ([§1](#spec-approvals)): the card, who asked and how (by the agent, or opened by the guard), its
  age, what and where (`push of feat/x
  in backend`), the summary, the rollback (a deploy without one says so), a deploy's card notes, a
  line for the agent and
  **Approve** / **Refuse**. A decision swaps the Inbox. Each request counts as one row; its card is
  not counted again under Cards. A half-typed line is held like an answer.
- **Worktrees left by runs:** after the upgrade to 0.12, the worktrees that runs created and did not
  remove ([§1](#spec-run)), each path with the hint to remove it with `git worktree remove <path>` in
  its repository. **Seen** (`POST /inbox/worktrees/seen`, board only, cookie and CSRF) deletes the
  list (the folders stay) and writes `inbox.worktrees_seen`. It counts as one row.
- One line "N new backlog items" linking to All backlog ([§12](#spec-backlog-board)); it counts as one
  row of the page, not in the badge.

- **Cards:** every open card that needs the owner, **once**, with each reason as a mark on its
  row: *waits for you* (`waiting_on` is one of `owner_names`, config, default `owner`, `me`,
  `you`; compared case-insensitively after trimming), *to accept n/m* (in `review` or `test` with
  a checklist; the row opens the task dialog on its Review tab), *n replies unread* (agent
  comments the owner has not marked read, with the newest reply's author and text), *n comments
  not delivered* (owner comments written more than an hour ago and still not delivered, with since
  when, why they wait as on the thread, and the oldest one's text) and *rule warning* (the
  warnings of its last `task.stage_changed`, until it moves again; the tracker-log warning is
  left out). To accept first, the longest waiting first (by the last stage change), then waiting
  on you, then the other reasons. A card whose only reason is waiting for the answer to its open
  question or approval request is not listed again here. A done card is listed only for agent
  replies written after it was closed: its rule warnings and older replies stay on the card, and
  moving a card to done marks its agent replies read (`comment.read` with stage `done`, by the
  mover).
- **Work log:** one line "N done cards without the work-log mark", only with
  `require_tracker_log = true` and only for projects that keep a tracker: a project keeps one
  once any of its cards has or ever had a tracker reference (`tracker_ref` or a row of its
  tracker history), or has a tracker link now (`tracker_url`; a removed link is not remembered). The line unfolds to the cards. An unlogged
  card archived by hand stays in it until its work log is marked.
- **Unlinked hook sessions** ([§4](#spec-hooks-linking)): one line "N hook sessions not linked"
  linking to Sessions; ended sessions are not counted (Sessions and Integrations still list them).
- **Limits** ([below](#spec-concurrency)): sessions running above the cap.
- **Policy proposals:** pending policy proposals ([§7](#spec-policy-proposals)), linking to
  Settings → Policy.
- **Rules proposals:** open rules proposals ([§6](#spec-rules-versions)), linking to
  Settings → Rules.
- **Rules changed directly:** versions made by `rules_update`, with reason and author, linking to the
  version in Settings → Rules, until the owner presses **Mark read** (event `rules.notice_read`).

Task rows open the task dialog (`/p/<slug>?task=<key>`). The sidebar badge counts the items that
each need the owner: cards (a card with an open question or approval request counts once, as
that), questions, approvals, policy and rules proposals and rules notices; the grouped lines (work log, unlinked
sessions, backlog, worktrees, limits) are rows of the page but not in the badge (hidden at zero); badge and list refetch on events that
can change them (`task.*`, `session.seen`, `session.status_changed`, `comment.added`,
`comment.delivered`, `comment.read`, `policy.proposed`,
`policy.proposal_*`, `rules.updated`, `rules.notice_read`, `rules.proposed`, `rules.proposal_*`, `review.step_*`, `backlog.*`, `concurrency.over`, `question.*` except `question.delivered`, `approval.requested`,
`approval.decided`, `approval.expired`), at most once per 2 s. With
nothing to show the page says "Nothing needs you right now."

<a id="spec-board-sessions"></a>

**Sessions** of a project are those its hooks placed in the project (by working directory) and
those bound to one of its tasks now; `/sessions` lists all sessions. Newest activity first, up to
200. Each row: status dot and hook status (working, waiting, idle, ended, or "no hook status"),
name, kind (desktop, hook or named), last hook activity and its age, working directory, "Open in
desktop" for a desktop session, the unlinked reason, and the bound tasks (each opens its task
dialog). Each row also has the **No live now**
switch ([§7](#spec-no-live)): a button `No live now`, or, while it is on, a `live off` mark (on a
row with no hook link yet it says the switch applies once the row is linked) and a button
`Allow live again`. The list refetches on session and binding events and on the switch, at most
once per 2 s.

<a id="spec-board-activity"></a>

**Activity** is the event log, newest first, 50 per page; "Load more" appends the next page
(`?before=<event id>`, also a plain link). Filters: project (`?project=<slug>`) and one event kind
(`?kind=<kind>`; an unknown kind is a 400). Hook status changes (`session.status_changed`) are
shown only when that kind is selected. Each row has the age, the kind, the task key or project as a
link, a short description (the same as `events_since`: `dev → test`, a report's summary, the
changed fields, a session's status change with up to three of its task keys and `+N`) and the actor. On the first page new events are
prepended live. Above the feed, **Actions on live** lists the newest 20 `live.action` events
([§7](#spec-journal)) for the project filter, refreshed live.

<a id="spec-board-audit"></a>

**Audit** is the journal of actions on live ([§7](#spec-journal)) as a table, newest first, 50 per
page; "Load more" appends the next page (`?before=<event id>`, also a plain link). Columns: time,
session, project, stand, tool, redacted command, decision, level, rule, task. Filters: project,
stand, session (its number, from the list), decision, period (1, 7 (the default), 30 or 90 days, or
All). The checkbox "Changes to the policy, stands and switches" (`?changes=1`) adds the `policy.*`,
`environment.*`, `session.no_live_on`, `session.no_live_off` and `session.terminal_opened` events as rows with their kind and a
short description, and with them questions and approvals. On the first page new rows are prepended live on `live.action`, and on those
kinds with the checkbox on. A note on the page says what it does not show: only tool calls that
touch a live stand are journaled, known by the names in its access map; test and local stands are
not. Events are never pruned. The session filter goes by the row the call was recorded under: calls
made before a guessed row was merged into its desktop session stay under the old row, whose name
they still show.

<a id="spec-usage"></a>
<a id="spec-brake"></a>

**Usage, token budgets and the brake: removed in 0.12** ([design
0025](design/0025-board-with-the-desktop.md)). The Usage page and its sidebar item, the background
reader of Claude Code transcripts and the transcript paths kept for it, token budgets (the Budgets
block, the task's budget row, budget lines through the hooks, budget notifications) and the brake
(Stop all, the banner, `relaybay brake on` refusals through the hooks) were built for agents on
cards; the desktop app shows the plan limits. Their tables were dropped by migration 0059
([§11](#spec-data)); their MCP tools were removed with MCP contract 2 ([§3](#spec-mcp-compat)). The
per-session **No live now** switch and the guard stay ([§7](#spec-no-live)).

<a id="spec-concurrency"></a>

*Concurrency.* `max_concurrent_sessions` (default 0, off), checked by a loop of its own every 60 s:
sessions `working` with activity in the last 10 minutes above it give one `concurrency.over` event
per 10 minutes and an Inbox line. It stops nothing.

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

**Server instructions** (sent on connect) are the working contract, seven numbered points in four parts, by when they apply. **At the start**: read `task_context` of your card (no card: `project_digest`; the whole board: `portfolio_digest`; more on a project: `project_snapshot`, whose first line names the rules); create a missing card with `task_create` and give it an area (the part of the project it is about; reuse a name from the project's areas, add one only when none fits); pass `session` (your `local_` id with `session_title`, else a name) and `hook_session` on writes; plan every task with `plan_set` (2-5 steps) and keep the Claude Code todo (TaskCreate / TaskUpdate). **While you work**: update `next_step`, `waiting_on` (only who acts next) and `progress` on every stage change and report; mark plan steps with `plan_step_update`, new work is `plan_step_add`; answer owner comments with `comment_add` or `comment_ack`; record decisions with `decision_add` and findings outside the task with `backlog_add`; read large documents by `doc_outline` and `doc_get` of a section. **At the end**: `report_add` with the configured first line, states and sizes (from the effective global rules); finished work goes to done, and review only when the owner wants a step-by-step acceptance (`review_steps_set`, `attach_file`). **Always**: fix what a `Warning (rules)` line names and re-read the rules on `rules updated to vN` (changes through `rules_propose`); `task_patch` for fields a cached schema lacks (a `Deprecated: …` line names the replacement); hosts and access come from the project's own notes, never paste secrets; a guard stop says why (`policy_get`, `policy_propose`).

<a id="spec-mcp-tools"></a>

**Tools.**

| Group | Tools |
|---|---|
| Projects | `project_list`, `project_upsert`, `project_delete` (coordinators only, [§2](#spec-archive-purge)), `project_snapshot` |
| Tasks | `task_list`, `task_get`, `task_create`, `task_update`, `task_patch`, `task_set_stage`, `task_doc_link` ([sections linked to a card](#spec-task-sections)), `task_archive`, `task_unarchive` (coordinators only, [§2](#spec-board-archive)) |
| Sessions | `task_bind_session`, `task_unbind_session` |
| Reports and decisions | `report_add`, `decision_add` |
| Comments | `comments_list`, `comment_ack`, `comment_add` |
| Review | `review_get`, `review_steps_set`, `review_step_update`, `attach_file`, `attachment_caption`, `attachment_get`, `annotation_reply` |
| Plan | `plan_get`, `plan_set`, `plan_step_add`, `plan_step_update`, `plan_accept` |
| Policy and audit | `policy_get`, `policy_propose`, `audit_list`; `environments_get` and `environment_propose` are stubs since 0.15 ([§7](#spec-environments-mcp)) |
| Catch-up and rules | `events_since`, `rules_get`, `rules_update`, `rules_propose` |
| Questions | `question_ask`, `question_list`, `question_withdraw` (the asker or a coordinator); none answers ([§1](#spec-questions)) |
| Approvals | `approval_request`, `approval_list`; none decides ([§1](#spec-approvals)) |
| Memory and documents | `decision_list`, `memory_add`, `memory_update`, `memory_search`, `memory_get`, `doc_list`, `doc_get`, `doc_update`, `doc_history`, `block_get`, `block_set` |
| Digests | `portfolio_digest`, `project_digest`, `task_context`, `project_pulse_set` |
| Backlog | `backlog_add`, `backlog_list`, `backlog_update`, `backlog_patch` ([§12](#spec-backlog-mcp)) |

Read tools are annotated read-only, write tools non-destructive; none reaches outside relaybay. The
digests are annotated as writes: `session` moves the caller's read position and can create or link
its session row.
Write tools take optional `session`, `session_title` and `hook_session`. `"-"` (or `""`) clears an
optional text field, because models cannot always send an empty string. `task_update`,
`task_patch` accept `expected_version` and fail instead of overwriting a newer version; otherwise the
last write wins. `task_create`, `task_update` and `task_patch` take `area` ([§1](#spec-areas)),
`task_create(bind=false)` creates a card without binding the calling session (it stays the event's
actor and its `hook_session` is linked as on any write; the default binds), and `project_upsert`
takes `archived` ([§2](#spec-project-archive)).
`task_list` filters by `area` (any project's area of that name without `project`); `task_get`,
`task_context`, `task_list` and the digests show a task's area, and `project_snapshot` lists the
project's areas with how many tasks carry each. `project_upsert` takes `repos`, `task_update` takes
`repos`, `prs` ([§1](#spec-task-code)) and `blocked_by` ([§1](#spec-task-blockers)); `task_patch` reaches them as `fields={"repos": […], "prs": […], "blocked_by": […]}` for a
client whose cached schema lacks them, and `relaybay://schema/task` lists them after the fields.

<a id="spec-mcp-resources"></a>

**Resources.** `relaybay://project/{slug}` (same markdown as `project_snapshot`),
`relaybay://project/{slug}/environments` (a stub since 0.15, [§7](#spec-environments-mcp)),
`relaybay://rules` and `relaybay://rules/{project}` (same as `rules_get`),
`relaybay://doc/{project}/{doc}` (a document's current markdown),
`relaybay://attachments/{attachment_id}/annotated` (a flattened PNG),
`relaybay://schema/task`
(the editable task fields as a table: name, type, description, how to clear), `relaybay://contract`
(the contract version, the rule below and the deprecations, [below](#spec-mcp-compat)).

<a id="spec-prompts"></a>

**Prompts.** `start_task` and `finish_stage`, with the arguments `project` (required) and `task`
(optional key), expand to one user message: the task's key, title and stage, a checklist and the
project's current rules (`rules_get` as text). `finish_stage` names what moving to the next stage
needs. An unknown project or task, or a task of another project, is a protocol error with the reason.
Checked with the MCP Python SDK client (`mcp` 2.3.0); whether a given client lists them as slash
commands is not verified, see [MCP tools](mcp-tools.md#prompts).

<a id="spec-mcp-warnings"></a>

**Warnings.** A write that misses a rule in `warn` mode succeeds and its result carries one line per
unmet requirement, `Warning (rules): moving to test usually needs a report since the last stage
change.` In `strict` mode the tool fails with `Rules: … Nothing changed.` `report_add` does the same
for a first line that does not fit `report_format` ([§6](#spec-rules-modes)). When the session's last
known rules version is older, a write tool's result also ends with one line, once per version:
`rules updated to vN: re-read relaybay://rules/<project>` ([§6](#spec-rules-versions)).

<a id="spec-mcp-compat"></a>

**Compatibility policy (MCP contract v2).** MCP clients keep the parameter schema of a tool they
already loaded, even across server restarts, so a release only adds: new optional parameters, new
tools, resources and prompts, a wider type, more enum values. Breaking, and allowed only when an
expired deprecation covers it: removing a tool, a parameter, a resource (URI or template), a prompt or
a prompt argument; narrowing what a parameter accepts (a type dropped, `null` included, enum values
dropped or an enum added, a tighter minimum, maximum or length, a field of an object parameter
removed or made required); a new required parameter or prompt argument, or an optional one made
required; a read-only tool that starts to write or a tool that becomes destructive. A field a stale
schema lacks is reached through `task_patch(task, fields={…})` or `backlog_patch(item, fields={…})`;
there is no such escape hatch for other tools.

A deprecation is an entry in `DEPRECATIONS` (`src/relaybay/mcp_server/contract.py`): the tool (or
resource URI, or prompt) and the parameter, if only one is meant, the version it is deprecated
since, a date it is removed not before, and the replacement. The window is at least one minor
version and 30 days: the entry expires once the server's minor version is above `since` and the
date has come. The tool is marked with `@deprecated` (or `@deprecated("<parameter>")`), which keeps
the signature and adds the line
`Deprecated: <tool> is deprecated since <version> and may be removed in a later minor version, not before <date>; use <replacement> instead.`
(for a parameter ``Deprecated: parameter `<name>` of <tool> is …``) to the tool's description and to
its result (for a parameter, only when the call passed it). `relaybay://contract` and the
[MCP reference](reference/mcp.md#contract) list the version, this rule and the deprecations
(`Deprecations: none.` in 0.8.0 and again under contract 2 until 0.15, which deprecates the
environment tools, [§7](#spec-environments-mcp)). The release that removes an expired
item raises `CONTRACT_VERSION` (1 in 0.8.0, 2 since 0.13); additions keep it.

**Contract 2: the 0.12 stubs removed.** 0.12 kept twenty tools and one resource of removed
features registered as deprecated stubs that did nothing ([§1](#spec-run), [§2](#spec-usage),
[§12](#spec-intake)): `run_start`, `run_status`, `run_events`, `run_settings`, `schedule_add`,
`schedule_list`, `schedule_cancel`, `usage_summary`, `budget_get`, `brake_set`, `brake_status`,
`intake_post`, `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`,
`message_send`, `messages_list`, `message_ack` and `relaybay://intake/{source}`. They were announced
as removable not before 2026-12-17; by the maintainer's decision contract 2 removes them earlier,
since they did nothing. A client with a cached schema that still calls one gets the tool error
`Unknown tool: <name>`. The baseline was rewritten for contract 2 outside `--rebaseline`, which refuses a
removal no expired deprecation covers.

`tests/fixtures/mcp_contract_baseline.json` holds the surface clients cache: tool names, input
schemas without descriptions, titles and defaults, annotations, resource URIs and templates, prompt
names and arguments. `tests/test_mcp_contract.py` fails on a breaking change against it that no
expired deprecation covers. Only a release pull request rewrites it, with
`scripts/gen_reference.py --rebaseline`, which refuses an uncovered breaking change and a covered
one without a raised contract version; `tests/test_release.py` checks it was written by the current
version. MCP protocol: what the pinned SDK (`mcp>=2,<3`) negotiates. Minimum
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
| `SessionStart` | working | `started (<source>)`; the answer adds "Your relaybay hook session: <id>. Pass it as `hook_session` in relaybay writes." to the agent's context (`hookSpecificOutput.additionalContext`), a line asking where it runs when relaybay cannot tell ([below](#spec-hooks-surface)), then the task context or a short project digest ([§8](#spec-digest-session-start)) |
| `UserPromptSubmit` | working | `prompt` (the prompt text is never stored) |
| `PreToolUse`, `PostToolUse` | working | the tool name (the tool input is never stored; `PreToolUse` is also checked by the guard, and a call that touches live leaves a redacted summary in the journal, [§7](#spec-guard)) |
| `Notification` | waiting when it asks for its user (`notification_type` `permission_prompt` or `elicitation_dialog`, or an unknown or missing type); idle for `idle_prompt` (about a minute after a turn ended) | the notification message |
| `Stop` | idle | `turn finished` |
| `SessionEnd` | ended | the session is unbound from every task it is bound to (history kept, `task.session_unbound` with `reason: "session_end"`), except after `/clear` or `/resume` (end reason `clear` or `resume`) and for desktop sessions, whose `local_…` id outlives the Claude Code process: the next `SessionStart` links the new Claude Code id to them ([below](#spec-hooks-linking)) |

Other events are ignored. Activity of a working session is written to the session at most once per
5 s (the latest wins); status changes are written at once. A `session.status_changed` event is
stored for each status change (working, waiting, idle, ended, with `previous`), and for a change of
the activity alone at most once per 60 s per session (an activity held back is stored when those
60 s end, unless another event came first): one event per project of the session's bound
tasks, with their `keys` and `project` (no `key`, no task id), or one without tasks for an unbound
session. The board refreshes the cards named in `keys`.

<a id="spec-hooks-messages"></a>

A `PreToolUse` of `SendMessage` (an explicit list of tools: another tool's `to` may be a mail
address) whose `tool_input.to` is a string records `session.message`, shown only in
[Activity](#spec-board-activity): sender, recipient and time, never any other part of the input. The recipient
loses control characters, its whitespace is collapsed and it is capped at 120 characters; one
holding `@` with a domain, `<`, a comma or a semicolon is not recorded, so no address list passes
(`name@team` is recorded). At most one event per sender and recipient per minute, and 20 recipients
per sender per minute. A failure here never stands in the hook's way.

<a id="spec-hooks-launch"></a>

A `PostToolUse` of the desktop tool that starts a session (`mcp__ccd_session__start_session`, an
explicit list) records `session.launched`: the calling session
and the new session's desktop id (`child`), nothing else of the input or the answer. The id is the
first `local_<uuid>` found in the strings of `tool_response`, which may be a string, an object or a
list of content blocks (at most 64 KiB of text, 6 levels deep, case folded); an answer without one,
or naming the caller itself, records nothing. Only [Activity](#spec-board-activity) shows the event,
one line per event. A failure here never stands in the hook's way. The `relaybay hook` forwarder
retries and spools this event like the todo tools'.

<a id="spec-hooks-launch-bind"></a>

**Binding at launch** (design 0026). The same hook creates the started desktop session when the
board does not know it yet (named by its id until it gives a title) and sets its
`session.launched_by` to the starter, once. It then reads the card keys in the tool's
`tool_input`: the strings only, whatever the fields are called, with the same bounded walk (64 KiB,
6 levels); only texts of the form `<slug>-<number>` of a project on the board (not archived) count,
not inside a longer word or slug (`x-acme-shop-4` and `acme-shop-4b` name nothing; case is folded;
`acme-shop-012` is `acme-shop-12`). Keys of cards that do not exist or are done are dropped.
Exactly one open card left, bound to nobody, to the starter or to the started session: the new
session is bound to it (`task_session.bound_by` `launch`, `task.session_bound` with `how: launch`
and the starter as actor), and the starter's binding on it ends with `unbound_reason`
`handed_over`. Nothing is bound when no open card is named, when several are, when the card is
bound to any other session (live, ended, or one without hooks: it is not the starter's to hand
over), when the started session is not new to this launch (a desktop session the board met more
than 10 minutes before the launch that has a live exact hook link, or one another session
started), or when a replayed hook (spooled) is older than the card's current binding. The
event gets `card` (the key bound) or `unbound` (why: `no open card named`, `<n> open cards named`,
`<key> is bound to <session name>`, `<desktop id> was started by <session name>`, `<desktop id> is
a live session started before this launch`,
`<key> was bound again after this launch`); no other text of the input is read or stored. A subagent's call carries its parent's Claude Code id, so the
parent is the starter; a `claude` its tools started (a nested process) is a plain session of its own
and cannot hand over its window's card. Who started the tool is otherwise not checked:
`task_bind_session` rebinds by hand. The order of this hook and the new session's own
`SessionStart` does not matter ([step 0](#spec-hooks-linking)).

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
   linked itself is never taken. A well-formed desktop id the board does not know yet, from an
   attended start (`"1"`), creates that desktop session (`session.seen`, named by its id) and links
   the Claude Code id to it exactly, so no `hook_session` is needed and comments are delivered from
   the first turn; an unattended or nested start, one that does not say (an empty or missing
   field, as from older desktop apps), or a Claude Code id another session's agent linked itself
   creates nothing and goes on with the steps below. Malformed ids are ignored.
1. Exact: a write passing `hook_session` (with `session`) links that Claude Code session id to the
   calling session; a row the hooks had created for it by guess hands over its status and is
   removed. An id that another live (not ended) session already linked exactly is not moved: the
   write succeeds and its response gets `Warning: hook_session <id> belongs to another session
   (<name>); not relinked.` A readable `session` name the board has never seen, passed with a
   hook id exactly linked to a live desktop session, is that desktop session (a launched session
   that does not know its `local_` id): no new row, no warning. `task_bind_session` never links a hook id (the caller may be binding
   another session); it answers with a note when one is passed. A bare Claude Code id passed as
   `session` that the hooks know (exactly linked, or the hooks' own row) resolves to that session
   with the note `Note: <id> is the hook session id of <name>; that session was used. …`, instead of
   creating a session named by the id; an unknown id stays an ordinary name.
2. Fallback on first sight: the one desktop session (not ended) bound to a task in the project
   whose `repo_path` contains the hook's `cwd`. A `SessionEnd` for an id no session holds is
   dropped.
3. Ambiguous (several candidates, the folder in several projects, or candidates already linked to
   other live hook sessions): nothing is guessed; the hook session stays **unlinked** with a reason
   and is listed on the Integrations page.
4. No desktop session in the project, or no project: a plain session named after the folder.

<a id="spec-hooks-surface"></a>

**Where the session runs.** The command hook adds Claude Code's `CLAUDE_CODE_ENTRYPOINT` to
`SessionStart` as `relaybay_entrypoint` (up to 60 characters are kept); the server records the
session's surface from it ([§1](#spec-session-surface)). An older hook client or an http hook
sends nothing and keeps working. A started session (not one a desktop session's tool started)
whose surface is still unknown gets one more sentence at the end of the session line: "relaybay
could not tell whether you run in the Claude desktop app or in a terminal. Tell it once, with your
next relaybay write on your task: task_patch(task, fields={"session_surface": "desktop"} or
{"session_surface": "terminal"}, session="<its name>", hook_session="<id>")."

<a id="spec-hooks-repos"></a>

**Repository links from the hooks.** Every applied hook event with a `cwd` (not a `SessionEnd`)
looks, in one indexed read, at the repositories of the tasks the session is bound to now. For each
such task the repository whose path is the folder or holds it (the closest one; a leading `~` or
`~/` is the user's home, `~name` is never expanded and matches only as written; compared with the
machine's case rules, nothing on disk is read) is linked with `how: auto` when it is not linked
yet, with the branch checked out there, unless another card not in done already holds that
branch in that repository (a shared working copy on another card's branch): then only the
repository is linked, with no branch and no worktree. While the task has no worktree for that repository, the
hook fills it ([§1](#spec-task-code)): the top of the working copy holding `cwd` (the nearest
folder with a `.git` folder or file, looked for no higher than the repository's path; none found:
the repository's path), so moving between folders of one checkout writes nothing, and a worktree
set by hand is never replaced. Only a new link, branch or worktree opens a write transaction,
separate from the hook's status write. A failure
of the link is logged and never fails the hook. Its `task.updated` event carries `how: "auto"`;
the digests' "changes since" leave such events out, the board refreshes the card as usual.

<a id="spec-hooks-down"></a>

**Never in the way when relaybay is down.** The command hook (`<python> -I -m relaybay.hook_client
--url … --token-file …`, standard library only) prints nothing on failure (only the server's JSON
answer on success), never uses a system proxy and always exits 0. It prints the server's answer
only when it is a `200` with a JSON object. http hook entries use a 10 s timeout (the approval
check of a push answers within 3 s of git reads plus the database; every other event at once, and
a server that is down refuses at once; a hung one makes each event wait up to 10 s) and get the same
JSON body; failures do not block the session, but Claude Code shows a non-blocking "hook error"
notice, and nothing is retried or spooled for them. `relaybay hook` is the same forwarder behind
the CLI.

<a id="spec-hooks-spool"></a>

**Retry and spool (command hooks).** The forwarder reads the event name (and the tool name of a
`PostToolUse`) from the raw body. `PreToolUse` (the guard) and every other `PostToolUse` (an
activity ping) get one try with a 0.3 s timeout and are never kept; a call that may need an
approval (by a rough check of the raw body: `git` with `push`, `gh` with `merge`, a program of
the guard's mutating rules, another server's MCP tool) gets its one try with a 5 s timeout
instead, its `PreToolUse` and its `PostToolUse`, because the approval check reads git. State-bearing events —
`SessionStart`, `SessionEnd`, `Stop`, `UserPromptSubmit`, `Notification` and a `PostToolUse` of
`TaskCreate`, `TaskUpdate` or `TodoWrite` — get one retry after 0.2 s, each try with a 1.0 s
timeout, the whole run inside 2.0 s counted from the forwarder's start (Claude Code ends the command
after 10 s; the interpreter's start-up and the spool write come on top). When neither try gets an
answer (refused, reset, timed out; an HTTP error is an answer) the event is appended to
`hook-spool.jsonl` in the desktop config folder (`RELAYBAY_CONFIG_DIR`, else `%LOCALAPPDATA%\relaybay`,
`~/Library/Application Support/relaybay` or `$XDG_CONFIG_HOME/relaybay`, worked out with the
standard library the way `platformdirs` does) with its time as `relaybay_spooled_at` (UTC ISO 8601)
and without the prompt text, stamped when the run started; the file is 0600 (also after a trim)
and keeps the newest 200 lines and at most 256 KB. Every later run except the guard sends the spool
first, oldest first, leaving its own event at least one try (state: 1.0 s per line, a ping: 0.3 s);
the sent lines leave the file and lines appended meanwhile stay. When a spooled line gets no answer
within its full timeout the rest stays, a ping is dropped and a state event goes to the spool at once
without tries of its own, so while the server is down only the first event waits for its two tries;
a line whose try was cut short by the budget says nothing about the server, and the event still gets
its own try. A short lock on `hook-spool.lock` beside it (`msvcrt.locking` on Windows, `flock`
elsewhere) is held only to read the file and to remove the sent lines, never over the network; the
drain waits for it at most 0.2 s and is skipped without it. An append waits for it up to 0.5 s
(less when the budget is nearly used); without it the line goes to a side file of its own,
`hook-spool.<pid>.<ns>.part` (an append of two processes to one file can tear on Windows), which the
next run holding the lock moves into the spool; `doctor` counts them too. A rewrite that fails
(Windows: the file is open in another process) removes nothing, and the run still sends its own
event. A run killed halfway, or two runs draining at once, send the same lines again; the server
skips them (below). `relaybay hook` counts the budget from the start of the `relaybay` process.

The server takes a spooled event (`relaybay_spooled_at` in the body) as the event at its own time:
when the session of its Claude Code id has an `activity_at` at or after that time, newer events got
through and the replay changes no status or activity (an old `SessionEnd` never ends a session that
started again, and unbinds nothing); a `SessionStart` among them still links the desktop session it
names. Otherwise it applies as a live event, with `activity_at` set to the event's time, so the
spooled events after it stay in order. A replayed todo change applies only when no item it touches
(the whole list for `TodoWrite`) was written at or after its time. A replay is answered `204` with
nothing: no owner comment is marked delivered to it and no context or Stop block is sent.
`relaybay doctor` reports the file as `hook spool` ([§5](#spec-doctor)); `setup --check` does not.

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
- When the owner accepts a plan on the board, the next `UserPromptSubmit`, `PreToolUse` or
  `PostToolUse` of the session bound to that task (linked as above) starts its answer with
  `Owner accepted the plan of <key> (<n> steps): go on with step <first step not done>.` (or
  `…: all steps are done.`), once (`plan.acceptance_delivered`). An idle session (it asked for the
  acceptance and ended its turn) is woken with the same line by the wake-up hook
  ([below](#spec-hooks-wake)). Not for an acceptance over MCP
  (`plan_accept` by the coordinator), nor for one made before the session was bound or more than
  2 hours ago (`ACCEPTANCE_TELL_HOURS`: after an upgrade or a long idle time it is stale); a plan
  turned back to `draft` is not told.
- A session named in `coordinators` (linked as above) is told every such acceptance on the board,
  in any project, on the same events and once per session:
  `Owner accepted the plan of <key> (<n> steps) on the board, bound to <session>.` (or `…, no
  session bound.`); for a task bound to the coordinator itself since before the acceptance it gets
  the line above instead (bound after it: this line). Within the same 2 hours; acceptances over MCP
  are not told.
- Acceptance lines take at most 2000 characters of one answer (`ACCEPTANCE_ROOM`), oldest task
  first; the rest stay untold until the next hook. Next to them an owner comment that does not fit
  in what is left waits for the next hook too (it is not marked delivered).
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

**Plan reminders.** For an exactly linked session (`cc_exact`), per card bound to it in `plan` or
`dev`: the current step ("Plan of acme-shop-12 (accepted): step 2/3 'Address book page' — Add, edit
and remove addresses.", or the next step when none is in progress; in `dev` with every step done
"the plan is closed (N/N done) while work goes on: add a step with plan_step_add or reopen one"; a
draft plan says "draft, not accepted yet"), plus "Now doing:" and the active form of the todo item
in progress; without plan steps the todo summary or, for a task in dev with neither, the hint to
write down what the agent is going to do. Only lines that ask for something: a card in `plan`
whose steps are all done gives none, and cards in `review`, `test` and `live` give none. A session
named in `coordinators` ([§6](#spec-rules)) never gets one: it reads plans and drafts in its
digests. At most three tasks, then one line on `plan_step_update`. Sent in `additionalContext` on
`SessionStart` (after the hook session line), on the `PostToolUse` of a todo change and every
`plan_reminder_every` `PostToolUse` events (default 30, `0` turns this one off; it repeats the
current text, only for cards in `dev`). A todo change never sends the same text twice in a row for
a session, compared without the "Now doing" part, so moving to the next todo item alone sends
nothing (`SessionStart` is a new context and resets it). A `PostToolUse` whose hook input names an
`agent_id` (a subagent's tool call; the field is unverified against a real payload) neither counts
nor gets a reminder. Owner comments in the same answer get the rest of the 8000-character budget.
When the session start adds the task context ([§8](#spec-digest-session-start)), that holds the
plan and the reminder counts as sent.

<a id="spec-hooks-wake"></a>

**Wake-up hook (opt-in; verified with Claude Code 2.1.309).** With
`relaybay setup --apply --wake`, `Stop` also runs `<python> -I -m relaybay.hook_client --url
<base>/hooks/claude-code/wait --token-file <data dir>/token --wait` as a command hook with
`"asyncRewake": true` and a 1800 s timeout. Claude Code runs it in the background; it posts the
`Stop` input to `POST /hooks/claude-code/wait`, which holds the request up to 25 minutes. When an
undelivered owner comment or an untold plan acceptance on the board
([above](#spec-hooks-comments)) exists for the exactly linked session while it is idle (and relaybay did
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
  relaybay.hook_client --url <base>/hooks/claude-code --token-file <data dir>/token`, 10 s timeout
  (the 5 s wait of a call that may need an approval, plus the start-up);
  from the installed program ([§13](#spec-desktop-setup)) the forwarder next to it instead,
  `<install folder>/relaybay-hook --url <base>/hooks/claude-code --token-file <token file>`.
  Needs nothing on `PATH` and no environment variable. Shell quoting: single quotes on macOS and
  Linux for paths with spaces, `$` or backticks; double quotes on Windows, where a path with `$` or a
  backtick makes the plan an error (nothing is written): both shells would expand it.
- `http` mode: Claude Code posts itself to `<base>/hooks/claude-code` with
  `Authorization: Bearer ${RELAYBAY_TOKEN}`, the host session headers
  ([§4](#spec-hooks-linking)) and `allowedEnvVars`, 2 s timeout; `RELAYBAY_TOKEN` must be set in
  Claude Code's environment. An http install made before 0.12 also sends `X-Relaybay-Run`; it keeps
  working and the server ignores the header.
- **Marker:** an entry is relaybay's only when it is a command hook running `-m
  relaybay.hook_client`, `relaybay-hook` (`relaybay-hook.exe`) or `relaybay hook` with `--url <scheme>://<host>:<port>/hooks/claude-code`,
  or an http hook whose URL path is exactly `/hooks/claude-code` on a loopback address or the
  configured host. The wake-up hook is relaybay's when it is that command with the
  `/hooks/claude-code/wait` URL followed by exactly `--wait`. No extra property is written; similar
  user hooks are never claimed.
- Nothing else in the file is changed, reordered or removed; indentation and line endings are kept;
  a symlinked file is written through to its target. Running apply again changes nothing; a new port,
  data folder or install updates only relaybay's entries, in place (the entries of a source checkout
  or another install become this install's, never a second set). An existing wake-up hook is
  rewritten with this install's forwarder too, so it never keeps a stale path.
- **Backup** before every write: `settings.json.relaybay-backup-<YYYYMMDD-HHMMSS>` next to the file
  (`-<n>` appended if the name exists).
- Refused: a file that is not UTF-8, not valid JSON, or whose `hooks` are not in the documented
  shape; an apply from an installed program that runs from a place that goes away
  ([§13](#spec-desktop-setup)). The token is never written into the settings.
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
`relaybay.hook_client` (a `relaybay-hook` answers `--version` with this relaybay's version;
another version means the hooks point at another install); in http mode, `RELAYBAY_TOKEN` set to the token in this environment; the
wake-up hook (an ok line naming the verified Claude Code version and the fallback risk, only when installed;
also in `doctor`); the MCP server registered for Claude Code.

<a id="spec-doctor"></a>

**`relaybay doctor [--json]`** (`--home`, `--port`, `--claude-home`) reads and never writes; unlike
`setup --check` it never creates the token and it reports Claude Code's files even when the server
is down. Items, in order: `relaybay` (`<version> (<install kind>: <executable>)`, the kind one of
`desktop-windows`, `desktop-macos`, `desktop-deb`, `docker`, `uv-tool`, `pipx`, `uvx`, `source`,
`pip`), `install` (kind and folder; fails when the installed program runs from a place that goes
away), `Python` (at least 3.12), `OS`,
`data folder` (exists, writable, whether `config.toml` is there), `data folder source` (`--home`,
`RELAYBAY_HOME`, `data_dir` in `desktop.toml` or the default), `server mode`
(`embedded (<base url>)` or `external <url> (desktop.toml)`), `hook executable` (the installed program's
`relaybay-hook` exists and answers `--version` with the same version; informational elsewhere),
`desktop` (start at login on or off and where, the `desktop.toml` path), `desktop start`
([§13](#spec-desktop-start-journal)), `hook spool` (`empty (<path>)` or
`N events waiting since <local time> (<path>)`, from `hook-spool.jsonl` in the desktop config
folder; it fails when the oldest event waits more than 10 minutes, [§4](#spec-hooks-spool)),
`token file` (present, not empty,
on POSIX not readable by others), `Claude Code` (on `PATH`, version at least the minimum supported),
`desktop app` (informational), then the server items of the check (`relaybay server`, `MCP with the
token`, `hook endpoint`; only the first without a token or when nothing listens), `rules hash`
(the hash of the global rules from the settings; fails when the running server started with another
one, reported in `/healthz`, i.e. it needs a restart; versions accepted since do not count), then the Claude Code items of the check (`hooks in user
settings`, `hook timeout` (fails for relaybay hook entries that wait less than this version
needs: 10 s for command and for http hooks; an older setup wrote 3 s and 2 s; for command hooks
the desktop program's start-up notice points to Settings → Reconnect, as for a forwarder that
does not run), `command hook runs` or `RELAYBAY_TOKEN for http hooks`, `MCP registered for Claude
Code`). Output: one `[ok]` / `[FAIL]` line per item, or with `--json` a list of
`{ok, name, detail}` (the shape of `setup --check --json`); exit code `1` when an item fails. Invalid
settings come out as one failed `settings` item. An ok item `ignored settings` names the keys
found in `config.toml` that are accepted and ignored since 0.12 (`max_running_turns`,
`min_free_memory_mb`, `claude_path`, `usage_enabled`, `usage_interval_seconds`,
[§10](#spec-config)).

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
three-item todo. `acme-shop` has a recent pulse note, `demo-api` one written three weeks ago.
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
| `stage_requirements` | per target stage, a list of `{kind = "report_since_stage_change"}`, `{kind = "field_present", field = "<task field>"}`, `{kind = "plan_accepted", unless_size = ["S"]}` or `{kind = "require_tracker_log"}` | `dev`: an accepted plan unless size `S`; `test`: a report since the last stage change; `done`: `progress` |
| `description_template` | markdown template for new task descriptions | none |
| `guidance` | free-text markdown for agents | one short paragraph |

<a id="spec-plan-rule"></a>

**Work by plan.** `plan_accepted` is met when the task's plan is `accepted` or its size is in
`unless_size` (default `["S"]`; a task without a size needs the plan), or when the task is
`inspect_only` (an analysis, a repro, a question answered by reading; set with `task_create` or
`task_update`, migration 0062): it goes to dev and done without a plan, and a report on it whose
state is `changed`, `committed` or `pushed` adds `Warning (rules): <key> is inspect-only, yet this
report says <state>: …`. In `warn` mode a move to dev
without it carries `Warning (rules): moving to dev usually needs an accepted plan (not needed for
size S).` plus `Write it with plan_set (2-5 steps), then ask for acceptance.` or, for a draft,
`The plan is not accepted yet: the owner accepts it on the board, the coordinator with
plan_accept.`; `strict` refuses the move. A move to dev of a task with no plan steps and no todo
also adds the MCP hint line `Hint: <key> has no plan and no todo yet: write down what you are
going to do (todo or 2-3 steps with plan_set).` (any size, not a rule warning).

**The stage follows the plan.** `plan_step_add`, and `plan_step_update` taking a done step back to
`in_progress`, on a task in `review` or `test` move it to `dev` in the same transaction: stage note
`rework: <reason>` (a reopened step: `rework: <step title>`), the usual `task.stage_changed`, no
stage rules checked; the MCP answer adds `acme-shop-12 moved from review back to dev (rework: …).`.
A task in `dev` stays. When a `plan_step_update` makes every step done on a task in `dev` with a
review checklist, the answer adds `Hint: all steps done and the review checklist is set: move the
task to review (task_set_stage).`; nothing moves to review by itself.

<a id="spec-rules-merge"></a>

**Layers and merge:** built-in defaults ⊕ the global `[rules]` table of `config.toml`, which is
where the global rules start ([versions](#spec-rules-versions)) ⊕ a project
override (a row in `project_rules`, the same document as JSON). `report_format` merges per key,
`stage_requirements` per stage (a list replaces the earlier one, `[]` drops the stage's
requirements), `description_template` replaces, the config `guidance` replaces the built-in text and
a project's `guidance` is appended. Every layer is validated (unknown keys, stages, kinds or fields
name the key). An error in `config.toml` stops the server at start; a broken project override is
logged once and ignored, and `rules_get`, the resource and the snapshot line say "project override
ignored: <error>".

<a id="spec-rules-versions"></a>

**Versions.** Each scope (`global` or a project) is a `rule_set` with numbered `rule_version` rows:
body (the global rules as a whole document, a project's override as a partial one), rendered markdown,
author, session, reason, a unified diff to the previous version and the time. Nothing is rewritten:
a rollback is a new version.

- **First start.** Migration `0018` only adds tables. At the first start after it, the rules from the
  config become global version 1 (author `config`) and every valid `project_rules` row becomes that
  project's version 1 (author `project_rules`), once, in one transaction and without an event. A
  project without a row has no versions (`project v0`) and follows the global rules. A broken row is
  not versioned; it stays logged and ignored until a new version replaces it.
- **`config.toml` stays a source.** The server remembers the hash of the raw `[rules]` table, before
  the merge with the built-in defaults (no table: the hash of `{}`), last turned into a version. At each
  start:
    - the same hash: nothing happens, and the current version stays in force even when it was changed
      since, so an accepted proposal survives restarts;
    - another hash: the file wins. The whole document becomes a new global version with author
      `config`, replacing what versions since the old one changed (they stay in the history and can
      be rolled back to) and a warning is logged; if the current version already equals it, only the
      remembered hash moves.

    Settings → Rules says when the current version differs from the file. A new relaybay release that
    changes the built-in defaults changes no table, so it never replaces accepted rules and makes no
    version: stored versions keep what they say, and new defaults arrive with the next change of the
    table. `rules_mode` and `require_tracker_log` are not part of the document and make no version.
- **Project rows.** A new project version also replaces the project's `project_rules` row, which stays
  the row checks read. A row edited by hand applies at once and becomes a new version (author
  `project_rules`) at the next start.
- **Broken versions.** A version that is not valid JSON or does not validate never stops the server, in
  any scope. The newest valid version is in force, without one the config (global) or the
  `project_rules` row (project). It is logged at start and Settings → Rules shows the error naming the
  row (`rule_version #7 (global v3): …`). It cannot be rolled back to. The next number comes from the
  highest number stored, so a hand-edited `current_version` cannot make two versions collide.
- **Namespaces.** Rule sets are found by project id, never by name. In a tool's `scope`, `global` is
  always the global rules; a project with the slug `global` is `project:global`.
- **`rules_update(scope, patch, reason, session…)`** makes a new version directly. It is only for
  sessions named in `coordinators` in the config, and it is a guard against mistakes, not access
  control: all MCP clients share one token. A desktop id entry (`local_…`) needs proof: the call passes
  `hook_session` equal to the id that session already has linked exactly; no MCP tool prints that id,
  and the same call's link does not count. So the session first links it with `hook_session` on an
  earlier write. A name or alias entry only needs to be passed as `session`. A malformed entry stops
  the start, naming the key. Everyone else gets an error pointing to `rules_propose`, and so does
  everyone when no coordinator is configured. A direct update shows in the Inbox until it is marked
  read, and in Activity.
- **`rules_propose(scope, patch, reason, session…)`** stores a proposal (`open`) for the owner, shown
  in the Inbox and in Settings → Rules. **Accept** merges the patch on top of the version current
  then, checks it again and makes a new version (author `<proposer>, accepted by <owner>`); a proposal
  that no longer validates or changes nothing is refused and stays open. **Reject** keeps it in the
  history. Only the board decides.
- **Patches** merge like `policy_propose`: objects key by key, other values (lists included) replace,
  `null` removes a key, so a project follows the global rules there and the global rules take the
  built-in default. Unknown keys are refused before merging, also with `null`, and the error does not
  repeat them. The result is validated as a whole; an invalid patch, one that changes nothing, an
  empty `reason` or a secret in `reason`, `guidance`, `description_template` or
  `report_format.first_line` is refused with the reason, on both tools and again on accept.
- **Events:** `rules.updated` (not for version 1), `rules.proposed`, `rules.proposal_accepted`,
  `rules.proposal_rejected`, `rules.notice_read`.
- **"Rules updated" line.** Per session the server keeps the newest global and per-project version it
  told that session about (`rule_seen`). When a write tool names a session whose version is older, its
  result ends with `rules updated to vN: re-read relaybay://rules/<project>`, once per version, and a
  global change only once whichever project comes next. Working this line out never fails the write:
  a problem is logged. The first write of a session records the
  current versions and says nothing. Writes without `session` say nothing; `task_bind_session` and
  `task_unbind_session` neither, their `session` is not the caller. The MCP `instructions` a new
  session gets on connect follow the current global version, and a connected session keeps what it
  got. Refreshing them uses a private attribute of the SDK: if that goes, a warning is logged and the
  start and the write are not affected.

<a id="spec-rules-page"></a>

**Settings → Rules** (`/settings/rules?scope=`): the scope's version line, hash, mode and rules as
markdown; the error of a broken version; the open proposals of all scopes with Accept / Reject and the
diff against the version current now (or why they no longer apply), refetched live; the scope's history,
newest first, with author, reason, a collapsible diff and "Roll back to vK", which asks for a
confirmation on the page and then makes a new version equal to vK; decided proposals. Everything an
agent wrote is escaped. Writes are form posts from a signed-in browser (cookie + CSRF).

<a id="spec-rules-hash"></a>

**Identity:** `Rules vN (global vG, project vP)`, where N = G + P: it grows with a change of either
layer, and P is 0 for a project without its own rules. The global scope says `Rules vG (global vG)`.
The rules hash is still the first 8 hex digits of SHA-256 over the canonical JSON of the merged
document plus `rules_mode`. `project_snapshot` starts with, and `portfolio_digest`, `project_digest`
and `task_context` contain,
`Rules v3 (global v2, project v1), hash 1a2b3c4d: accepted plan before dev, report before test, progress before done (read relaybay://rules/<project>)`;
`rules_get` and the resource have the same
version in a `version` line.

<a id="spec-rules-modes"></a>

**Checks and modes:** requirements are checked on stage moves (`task_set_stage`, `task_update`
/ `task_patch` with a stage, the board form). `rules_mode = "warn"` (default): the move succeeds and
the response, the board form and the `task.stage_changed` event carry the warnings.
`rules_mode = "strict"`: the move is refused with the reason and nothing changes.

`report_add` checks the first line of the report against the project's `report_format`, when its
`first_line` is the standard `state · size · gist`: it has to have that shape, with a state and a size
that format allows. A custom `first_line` is free text for agents and is not checked. In `warn` mode the report is saved
as always and the result and the `report.added` event carry
`Warning (rules): the first line of a report should be …`; `strict` refuses it with `Rules: … Nothing changed.`
An M or L report without a `verified: <where>` mark ([§1](#spec-report-marks)) only warns, in both
modes.

**Tracker log.** `{kind = "require_tracker_log"}` is met when the task's `tracker_logged` is set after
the write. `require_tracker_log = true` in the config adds it to `done` in every scope, on top of the
document and without a version, and it shows in the rules line and `rules_get` like any requirement:
in `warn` mode a move to done without the mark carries the warning, in `strict` it is refused.
In a project that keeps no tracker (no card has or ever had a tracker reference, none has a
tracker link now, and the write sets neither) the requirement asks nothing: no warning, no
refusal.

<a id="spec-environments"></a>

## 7. Environments and access

Design: [Environments and access](design/0006-environments-and-access.md),
[Policy as data](design/0013-policy-as-data.md).

Since 0.15 the board has no page for environments and agents have no tool for them: hosts and
access live in each project's own notes (owner, 2026-11-19). The environments, access entries and
proposals stored before stay in the database and nothing edits them any more; the guard, the
approvals and the live journal below still recognise the stored environments, and the policy
(Settings → Policy) still governs them. A project without environments is not guarded by name.

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
`secrets.disable`, `secrets.allow`, `approvals.disable` and `approvals.ignore` may also be written `key+` (add; a pattern with an existing
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

Every preset has the same `approvals`: `push`, `merge` and `test` true, `commit` and `desktop`
false, `live` true, `valid_minutes` 120 ([approvals](#spec-approvals)).

Validation: JSON Schema 2020-12 plus checks the schema cannot express — `preset` only in the global
layer, custom pattern ids unique and not a built-in id, `approvals.live` only `true` ("anything
touching a live stand needs the owner's approval; a policy cannot drop it"), custom regular
expressions (`command`, `change`, `secrets.allow`, `approvals.ignore`) at most 200 characters, without back-references, without a quantified
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
An assignment with nothing after `=` is a parameter name, not a value: `name=` followed by the end
of the text, a line break, `&`, `)`, `<` (a placeholder such as `<token>`, which also lets a value
written in angle brackets pass) or a closing backtick passes (`/admin-invitation?token=`,
`?token=&next=1`, an env-file line `KEY=`); spaces after `=` end the value only for a URL's query
parameter (`?token= …`). A value after `=` on the same line, quoted or after spaces
(`?token=a1b2…`, ``password=`x` ``, `password= "x"`) is refused as before.
Quoted values count whole. The journal masks exactly these places, from the same table
(`_SECRET_SPOTS`), whatever the policy switches off. A value that is a reference as a whole
always passes, and so does a value, or a detected secret, that one of the policy's `secrets.allow`
patterns matches whole. The refusal names the field and the detector (`notes looks like it
contains a secret value (jwt)`) and never repeats the value. The checks of a write use the
effective policy of the environment (or, for a new environment, of the project). These are pattern checks: a secret in an unusual shape (e.g. in plain prose) can pass.

<a id="spec-test-secrets"></a>

**Test secrets.** When the policy allows them for the environment's kind
(`secrets.test_secrets.local` / `.test`; the schema has no `live` key and the service refuses them
on live whatever the layers say), an access entry may hold harmless test values: stored in its
fields JSON as `{"value": "...", "test": true}`, exempt from the secret checks, at most 20 per
entry, names in lowercase like other fields. They are entered on the board only (a "Test secrets"
box in the access form, shown when allowed or when the entry has some; `name: ***` keeps a stored
value, a removed line removes it, a form without the box keeps them) before 0.15; no tool shows
them to agents since then, so `expose_test_secrets_to_agents` has no effect. An environment with
test secrets cannot become live. The
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
rises above `read` (live → test with the default preset turns `read` into `full`). These writes
had a form on the board until 0.15; the service keeps the rules for the stored environments.

<a id="spec-environments-page"></a>

**Stands & access page: removed in 0.15.** The page (`/p/<slug>/environments`) listed the
environments with their access entries and levels, with forms to edit them and the agents'
proposals with **Accept** / **Reject**. Its repositories, areas and archive moved to the project's
Settings page ([§2](#spec-board-repos)); the old address redirects there (`301`); the environment
and proposal writes are gone (`404` / `405`).

<a id="spec-proposals"></a>

**Proposals: removed in 0.15.** `environment_propose` recorded a proposal for the owner
(`environment.proposed`, `environment.proposal_accepted`, `environment.proposal_rejected`); since
0.15 it records nothing ([below](#spec-environments-mcp)), the Inbox no longer lists environment
proposals, and pending ones stay in the table `environment_proposal` undecided. The Audit page and
`audit_list` still show the old events.

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
owner.`) is shown to Claude for `deny` and to the user
in the permission prompt for `ask`. In `warn` mode the call goes on and `additionalContext` carries
the same reason ending `(warn mode: not blocked)`. relaybay never answers `allow`, since it would
skip Claude Code's own permission prompt. When the level lets a call through, the approval check
follows for a session it applies to ([approvals](#spec-approvals)): a push, a merge or a stand
change without an unused approval is denied. Owner comments for an exactly linked session are added to
`additionalContext` of the same answer. A guard error is logged and the call goes on; relaybay down
or slow means no answer at all ([§4](#spec-hooks-down)).

**The guard is a safety net against mistakes, not a sandbox.** It knows only the names in the
access map; a host reached by an IP address, another alias, a script, an environment variable, a
tool other than Bash and MCP, or with the guard off is not recognised, and the heuristics both miss
changes and flag harmless commands.

<a id="spec-no-live"></a>

**No live now.** The owner's switch per session, on the Sessions rows
(`POST /sessions/{id}/no-live`, cookie and CSRF; no MCP tool). It is stored on the session
(`no_live_at`, `no_live_by`) and recorded as `session.no_live_on` / `session.no_live_off`; a repeat
of the current state records nothing. While it is on, any touch of a `live` environment by that
session, reads included, is answered with `deny`, whatever the environment's level and guard mode
(`off` too): level `none`, rule `no-live-session`, and a reason that starts "the owner switched live
off for this session", names the stand and the matched identifier and says only the owner can switch
it back. The call is journaled like any other. The policy does not change, cannot disable the rule,
and applies again once the switch is off. The live environments of every project count, not only
those of the session's project and tasks; other environments keep their normal decision. A guessed
hook row that is merged into a desktop session hands its switch to that session, with a
`session.no_live_on` event carrying `carried_from`; the old row keeps no switch. A failure to write
the journal is logged and the decision still stands.

A row with no hook link yet (`cc_session_id` empty, most named sessions) denies nothing, because the
guard finds a session by its Claude Code id; the page says so and the switch stays set for when the
row is linked. While the switch is on, an agent's write with a `hook_session` does not move the row
(a warning instead) when the hooks never saw that id, on any row, linked or not, or when the row is
exactly linked and not ended: either would leave the real hooks a fresh row without the switch. A
real id the hooks saw still moves a guessed row or an ended exact one, and the switch goes along.
The desktop app reporting a new id for its window (`/clear`, resume) always moves it, and the switch
stays with the row. When an ended row named after its folder is reused for a new session, no new
row appears and the switch is cleared with a `session.no_live_off` event carrying `reason`: it was
aimed at that session, not at the folder.

It is a revocation for the session's next tool call, on top of the same safety net: it applies only
to the names in the access map, not to a command already running; a guard error or relaybay down
means no answer ([§4](#spec-hooks-down)); a `claude` started from inside the session has its own row
and its own switch. Also:

- `/clear` in a terminal `claude` gives a new Claude Code id. A row named after its folder is reused
  with `session.no_live_off`; any other row stays ended and the new id gets a new row without the
  switch, until the agent's write with that real id moves the switch there.
- A wrong guess stays correctable only with an id the hooks really saw: an agent that names another
  real id of a guessed row can move it there, and the hooks of the old id then have no switch.
- "Only the owner" holds for the tools. An agent with a shell can read the board's token from the
  data folder and call the board, or forge hook events (`relaybay hook` reads the token itself) and
  the hook's environment; the switch does not stop a determined agent.
- A host shared between a test stand and another project's live stand is denied too, since the name
  is a live name; a harmless `grep api-prod` that names a live stand of any project counts as a
  touch.

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
all), the task dialog and page show the task's own as **Actions on live**, the **Audit** page
([§2](#spec-board-audit)) shows them all with filters, and `audit_list` returns them to agents.

<a id="spec-environments-mcp"></a>

**MCP.** Since 0.15 `environments_get` and `environment_propose` stay registered with their
parameters as deprecated tools that do nothing, within contract 2 ([§3](#spec-mcp-compat)): each
answers `Nothing done: this tool does nothing since 0.15.` and `Deprecated: environments and access
were removed in 0.15; environments are kept in the project's own notes.`, which also ends its
description; the resource `relaybay://project/{slug}/environments` answers the same. They are
removed, and the contract version raised, by the first later minor release from 2026-12-19.
`project_snapshot`, `project_digest` and `task_context` no longer list environments.
`policy_get(project?, environment?)` returns the effective policy of
the global scope, a project or an environment: the layer chain and one line per value with the
layer that set it (`- guard.mode = "warn"  (preset:default)`), plus ignored broken layers. There is
no write tool besides `policy_propose`.

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
token; a Bearer request is refused (`403`), as on the project's Settings page. A change widens agents' access
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

Steps 11.6a to 11.6c of [Memory and context](design/0008-memory-and-context.md): documents,
decisions with supersede chains, memory notes, blocks, full-text search and the Docs & rules page;
the project pulse, digests per level with budgets and reader cursors, and the context a session
gets at its start ([below](#spec-digest)); markdown import and export
([below](#spec-import)). Vectors are not built yet. **Visibility per role** (coordinator everything, a manager its project, a
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
its heading and everything until the next heading of the same or a higher level. An **explicit
anchor**, `<a id="name"></a>` alone on a line directly above a heading (blank lines between
allowed), is an alias of that section: `doc_get`, links, search hits and the page accept either
name, and the alias survives a reworded heading. An alias equal to another section's anchor is
ignored. With `section`, the new text replaces that section: starting with a heading it replaces
the heading too, otherwise only the text under it. Only the section's own lines change: the blank
lines that end it (and, for text without a heading, those right under the heading) stay as they
were, so a section replaced by its own text leaves the body byte for byte. With `lines` (`a-b`,
1-based, inclusive) and `expected` (their text as read), only those lines are replaced (`body` `-`
removes them) when they still read as `expected`, both compared without trailing blank lines;
otherwise the answer is a conflict with their current text. A replacement keeps the range's
trailing blank lines; the result is stored without outer blank lines, and a write that would leave
the document empty is refused. A line write guards itself with `expected`: a stale `base_version` alone is no
conflict there (decided by default). A database document holds at most 200,000 characters; a
larger one is a file document ([below](#spec-documents-file)). A diff between any two versions is
`difflib.unified_diff`.

<a id="spec-documents-kinds"></a>

**Kinds.** The well-known kinds are `spec`, `status`, `task`, `brief`, `runbook`, `adr` and `retro`;
any other kind is free text (default `doc`). Documents are free markdown: no section is structured,
and decisions have their own table ([below](#spec-memory-decisions)). A task's document is named
`task-<ref>` (import makes it so); a task created on the board or through MCP gets one with
`doc_update` and `task_update(doc_path="relaybay://doc/<project>/task-<ref>")`. A `task_create`,
`task_update` or `task_patch` that sets `doc_path` to an absolute path of a local `.md` file that no
file document of the project reads yet makes that file the file document `task-<key>` (kind
`task`, writes through relaybay off, the same path checks as `doc_update(source_path)`); the answer
says so in one line with the board link, or why not (a refused or missing file, the name
`task-<key>` already taken). The task write itself never fails over it, and the file is only read.
The board's task form has no document field: `doc_path` is set through MCP (and by import).

<a id="spec-documents-repo"></a>

**Documents that live in a repository.** `document.source_url` (an http or https link, at most 500
characters, secret-scanned) marks a document whose source is a repository. `doc_update` sets it with
`source_url` and removes it with `"-"`; a write to a linked document is refused without `force`, the
error naming the link, and so is body `"-"` on an existing document (it would drop its text). Like
`tracker_url`, the link may hold no control or invisible character (Unicode `Cc`, `Cf`). A linked document may have no text of its own (`body` `"-"`, stored as an
empty version); an unlinked one may not. The board shows "lives in the repository" with the link and no
edit, `doc_get` and `doc_list` name the link, export leaves a linked document without text out of the
layout (with text it is written as usual), and import lists a changed file of a linked document as
`skipped` ("lives in the repository"), also with `--overwrite`.

<a id="spec-documents-file"></a>

**File documents** (design 0026). `doc_update(base_version=0, source_path=<absolute path>, body="-")`
or **New document: a file on disk** on Docs & rules adds a document whose text stays in a file on
the machine relaybay runs on, inside a repository or not; relaybay keeps no second copy, and writes
the file only when writes through relaybay are on for that document
([below](#spec-documents-file-writes)). The path must be absolute and resolve (links first) to a regular file; over 8 MB
or not UTF-8 is refused. The resolved path, never the given one, is then checked against a deny
list (decided by default, the most reversible way): the data folder at any depth except its
`worktrees/`, the token file and `desktop.toml`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`,
`~/.docker`, `/proc`, `/sys`, `/dev`, and file names (any case) `.env*`, `*.pem`, `*.key`,
`id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `*.kdbx`, `credentials*`; a network share or device path is
refused before the disk is touched and again after resolution (a junction to a share), unless
`attachment_roots` names that share. The stored path is the resolved one, and every later read
resolves it again: a file swapped for a link into a denied place is refused ("refused now"), its
last index kept. The file is read and indexed before the write transaction opens. Every read
compares the file's size and modification time with the last read, hashes it when
they differ (SHA-256 of the bytes) and, when the hash differs, rebuilds the section index and writes
one `document.updated` event (actor `file`, `file: true`, `hash_before`, `hash_after`,
`sections_changed` and the anchors `added`, `removed`, `changed`, at most 20 each, compared by the
hash of each section's own text). Nothing watches the disk. The text is not versioned:
`doc_history` lists those events, `doc_get` names the file, the hash and the version. A missing file keeps its
document and its last index, marked "file missing since …" (`document.file_missing_at`); reads say
so and give the pointer only; the file coming back clears the mark. A secret a
detector of the project's policy finds is masked in the index on every line it covers: a line that
matches on its own, and every line of a match only the whole text shows (a private key from its
BEGIN line to its END line or the end of the text, a flag and its value on two lines). A line
that matches on its own keeps `redact`'s version only when no detector finds anything in it
again; every other masked line, and every line only a match across lines covers, becomes a fixed
mark (a heading `## [masked]`, keeping its level). Titles,
heading paths, anchors and aliases are taken from the masked lines, so the index, the outline,
search hits, a card's pointer and `document.updated` anchors never carry the value. The count is
`document.masked_lines` and the outline names it; the document is not refused, and reads pass the
file's text through. Finding them is linear in the text (one pass per line, one over the whole
text). Export leaves file documents out; import lists a
file of the layout whose document is a file document as `skipped`. The answers name the file as
`<repo>:<path>` when it is inside a repository of the project, else by its path. Not built: a
worktree session mapping the path to its own copy.

<a id="spec-documents-file-writes"></a>

**Writing a file document** (design 0027, part A). A file document's `version` counts the hashes
its file has had: 1 when it is added (0.14 documents moved from 0 to 1 by migration 0054), one more
for every write through relaybay and for every change made outside relaybay that a read notices.
Writes through relaybay are **opt-in per document** (`document.file_writable`, off by default and
for every document 0.14 added): `doc_update(file_writes="on"|"off")` switches them, at the add or
later (with `body` `"-"` that is all the call does), for coordinator sessions only (the check of
`task_archive`); the switch is one `document.updated` event with `file_writes`. With writes off,
`doc_update` on the document is refused, also with `force`, naming the file and, with `section`,
its lines, and saying that writes through relaybay are off. With writes on, `doc_update` takes
`section`, `lines` + `expected` or the whole text, as for a database document. Unlike a database
document, the body is written as it is (leading blank lines, indents and trailing spaces kept);
only a CR before LF and the final newlines are dropped, and the file keeps its own final newlines:

1. The path rules are checked again (as for a read); a missing or unreadable file refuses the
   write and names why. The file's bytes are read and hashed; a hash relaybay did not see is an
   outside change, recorded first (below), so the version moves on.
2. A `base_version` other than the version is a conflict whose message holds the current section
   (or text); `lines` + `expected` write whatever the version, while the lines still read as
   expected. The new text is checked for secrets like any `doc_update` body; the body of one call
   holds at most 200,000 characters.
3. The change is spliced into the bytes: only the lines that differ are written again, every other
   line keeps its bytes and its line ending. A new line gets the ending of the line where the change
   starts (at the end of the file, the line before it; else CRLF when the file has any). The byte
   order mark and the newlines at the end of the file stay exactly as they were, so a section
   replaced by its own text leaves the same SHA-256 and writes nothing. A result over 8 MB is
   refused.
4. The bytes go to a temporary file in the same folder (the file's permissions copied), which
   replaces the file (`os.replace`). Right before the replace the file is hashed again; when it
   changed in between, nothing is written, the change is recorded as an outside one and the answer
   is the conflict with the current text.
5. The new hash, size and time are stored, the index is rebuilt and one `document.updated` event
   (actor: the session; `file`, `version`, `reason`, `hash_before`, `hash_after`, the changed
   anchors, `section` or `lines`) is written; `document.file_changed_by` becomes `relaybay`.

The write does not commit; a file in a git working tree shows the diff of the replaced lines.
**An outside change** (an editor, a script, a session's own file tools) is never prevented: the
read that sees the new hash writes `document.updated` with `outside: true` and the changed anchors,
and sets `document.file_changed_by` `outside`, `file_changed_at` and `file_changed_sections` (the
anchors of every outside change since the last write through relaybay, at most 20: the added
and changed sections the file still has, the removed ones as `-anchor`, named as a section again
when it comes back). While it is `outside`, `doc_outline`, `doc_get`, the `doc_search` hits of the
document and the linked-section lines of `task_context` add `Note: <doc> was changed
outside relaybay at <time>: sections a, b; removed c (version N).` (`doc_search` gives one short
line per changed document after its hits: `Note: <doc> was changed outside relaybay at <time> (N
sections, version N); doc_get(project, '<doc>') names them.`); the next write through
relaybay clears it. The answers of `doc_get`
and `doc_outline` name the version and `writes through relaybay: on` or `off`. The board's page of
a file document stays read-only.

<a id="spec-documents-index"></a>

**The section index.** `document_section` holds one row per section of a document's current text
(database documents on every write, file documents on a changed hash; rebuilt at start for
database documents that have none): position, anchor, alias, level, title, heading path
(`Spec > Payments > Gift cards`), first and last line (with the subsections), the last line of its
own text, characters, bytes and the hash of its own text; the text before the first heading, when
it is not blank, is position 0 with level 0. The searchable text (each section's own text, secrets
masked, with the heading path) is the FTS5 table `doc_section_index` the service creates, like
`search_index`, or the plain table `doc_section_text` without FTS5; a count mismatch at start
rebuilds it. `doc_outline(project, doc, depth?, under?)` lists the headings: level, anchor (and
alias), title, lines `a-b`, characters and the token estimate (bytes / 3.5). `doc_search(project,
query, doc?, limit?)` returns hits per section (every word or its stem in the section's own text or
heading path; FTS5 rank, else in document order): document, anchor, heading path, the best line
with the non-blank lines next to it, the section's lines and size, and the file. `doc_get` takes
`lines` (`a-b`) and `max_chars` (2,000 to 1,000,000): a read over the cap returns the first lines
of the range, the outline of its top two heading levels inside it as far as half the cap allows
(the rest as `… N more heading(s): doc_outline(..., under=…)`) and `Read on: doc_get(...,
lines="x-y")` with the next range that fits; a single line longer than the room is cut and said so.
The whole answer is measured as written and never over the cap: the outline loses headings
first, then the text loses lines from its end; long names in the header are clipped. Never a
silent cut. The default cap is 40,000 characters, only for a
document over 200,000 characters (a file document); a smaller one reads whole unless `max_chars`
is passed. `memory_search` keeps one hit per document and adds the anchor of the section that holds
its line.

**Board links in MCP answers.** Next to the `relaybay://doc` names, the answers give the board
address a person opens: `<board>/p/<project>/docs/<doc>#<anchor>`, where `<board>` is `board_url`
(config) or else the server's own `http://<host>:<port>` (a wildcard bind gives `127.0.0.1`).
`doc_list` adds `board <link>` per document; `doc_outline` one `Board:` line with the document link
and the section pattern (not a link per heading); `doc_get` names the link of the document or
section in its header comment; each `doc_search` hit ends with its section's link; the pointers of
`task_context` (and `task_doc_link`) end with theirs; `task_get` adds it to the `doc` field when
the field leads to a document. An anchor over 100 characters links the document instead, so a
capped `doc_get` still fits its cap. A section's link uses its explicit anchor when it has one (the page
carries it as an id and it outlives a reworded heading), else its heading anchor; letters of any
language stay as they are.

**GitHub-style anchors.** GitHub and the editors that follow it make an anchor by turning every
space into a dash without collapsing runs (`## 2. Orders / Returns / Refunds` → `2-orders--returns--refunds`;
relaybay's is `2-orders-returns-refunds`). A section asked for by an anchor with two dashes in a row
(`doc_get(section)`, a `relaybay://doc` link, `task_doc_link`) that no heading or explicit anchor
has is the heading whose GitHub anchor it is, else the heading whose anchor equals it with the
runs of dashes made one (a relaybay anchor never holds two dashes in a row, so at most one). The document page gives each heading whose GitHub anchor differs (and is not another
section's name) an extra id with it, so the links inside a document written for GitHub land too.

<a id="spec-documents-edit"></a>

**Editing on the board.** The document page has **Edit** (the whole markdown) and an `edit` link at
each heading that starts a section (that section only, `?section=<anchor>`), neither for an older
version or a linked document. Heading ids and edit links come from the section list above, matched
by line: a heading it does not count (setext, inside a quote) gets no id and no edit link. The form holds the text, a required reason and the base version; a save goes through
`doc_update` with author `owner`, from a signed-in browser only (board cookie and CSRF, no Bearer).
A conflict answers 409 with the typed text and reason kept, the current text (or section) shown to
merge and the base version moved to the current one, both from one read, so the next save is the
merge; a section that is gone by then turns the form into a whole-document edit, with a note; another refusal
answers 400 with the text kept. **New document** on Docs & rules creates one (name, kind, title,
optional repository link, text, reason) the same way.

<a id="spec-documents-delete"></a>

**Deleting a document.** `doc_update(delete=true)` deletes a project document; there is no tool of
its own. `base_version` must be the current version (a stale one is refused as a conflict that
names the current version, nothing deleted), `body` is ignored (pass `"-"`) and `reason` is
required (secret-scanned). Any session may delete a document, except one of kind `spec` or `status`,
which holds the project's rules: that one only the owner on the board or a coordinator session
(the check of `task_archive`) deletes, and a change document named `spec-<task key>` (deleted after
its task is deployed) also the session bound to that task of the project; the refusal says who may.
In one transaction relaybay writes the export ([§2](#spec-archive-purge)) with the document's row,
all its versions, its sections, the cards' section links (`task_doc_section`) and its
`import_item` records (`document` in the export names it), then deletes them with the search rows
of the document and its sections. A file document loses only its rows: its file on disk is never
touched. The event `document.deleted` (the actor, `doc`, `title`, `doc_kind`, `version`,
`reason`, `file` for a file document, `export`, row counts) is on Activity, refreshes Docs & rules
and, with "changes", is on the Audit page. A card whose document field pointed at it shows the
link as `no such document` ([below](#spec-documents-links)). The name is free again afterwards.
The document page has **Delete document** (folded): an optional reason (default `deleted on the
board`), the document's name typed again and one confirmation (`POST
/p/<project>/docs/<doc>/delete`, fields `confirm`, `base_version`, `reason`; board cookie and
CSRF, no Bearer). A wrong name, a stale version or an export that could not be written comes back
to the document page with the reason; done, it goes to Docs & rules with the export's name; a
repeat after the deletion says `There is no document <doc> (any more).`

<a id="spec-documents-import"></a>

**Importing a file once.** `doc_update(from_path=<absolute path>, body="-")` reads a local text or
markdown file once and saves its text as the whole body of a database document, so an agent does
not send a long text through `body`. It creates the document (`base_version` 0) or writes an
existing database document whole (`base_version` its current version, else the usual conflict
with the current text). It does not go with `section`, `lines`, `source_path`, `source_url`,
`file_writes` or `delete`, nor into a file document (edit its file instead). The path passes the
same checks as a file document's `source_path` ([above](#spec-documents-file): absolute, links
resolved, a regular file, no network share outside `attachment_roots`, not in relaybay's data
folder, not a protected or credentials file); the text is UTF-8 (a byte order mark dropped, CRLF
read as LF, outer blank lines dropped), at most 200,000 characters, not empty, and secret-scanned
like `body`. The document keeps no link to the file: a later change to the file does not reach it.
The answer adds `Imported from <path> (N characters).`

<a id="spec-documents-link-check"></a>

**Links without a target.** A `doc_update` that writes a document's text (a database document, or
a file document written through relaybay) and a `doc_get` of a whole current document (no
`section`, `lines` or `version`, not capped) end with one line
`Warning: links without a target: <link>, … (N)` when the text has links that lead nowhere:
`relaybay://doc/<project>/<doc>[#<anchor>]` links of any project on the board whose document or
section is missing, and in-document markdown links `[text](#anchor)` to a section the document
lacks (listed as `<doc>#<anchor>`). Sections are looked up as the board looks them up (anchor,
explicit anchor, GitHub-style anchor, [above](#spec-documents-index)); links inside fenced
code and code spans are examples and not checked. At most 10 are named, then `… and N more`; each
link once. Nothing is refused.

<a id="spec-change-documents"></a>

**Change documents.** The document `spec-<task key>` of a card's project (`spec-acme-shop-12`) is
that card's change document: what the card changes in the main spec before it is deployed, as a
delta per section, found by its name (no tool or field of its own). A `spec-` document whose name
is no card key of its project is an ordinary document. Format: the text before the first `## `
heading (a title, a summary) is no change; then one `## <doc>#<anchor>` heading per changed
section of a main document, naming that section by its anchor (or explicit anchor); under it one or
more blocks, each starting with a line `ADDED:`, `MODIFIED:` or `REMOVED:` (text may follow on the
same line). `ADDED:` text goes below the section's own text; `MODIFIED:` text replaces it, and a
line `Was: …` in it quotes the old value and is no part of the new text; `REMOVED:` deletes the
section (text under it is a note). A new section is an `ADDED:` block under its parent whose text
starts with its own heading one level below the parent's. Lines inside fenced code are text.

```markdown
# Gift cards v2

## spec#gift-cards
MODIFIED:
Cards expire in two years.
Was: Cards expire in a year.

ADDED:
### Gift card refunds
Within 14 days.

## spec#vouchers
REMOVED:
```

The format is checked softly, never refused: a heading that is not `<doc>#<anchor>`, an unknown op
(a word in capitals with a colon alone on its line), text under a target before its first op, a
target without a block, `ADDED:` or `MODIFIED:` without text, a document without targets, and a
target whose document or section does not exist are problems. A write of a change document
(`doc_update`, the board's edit and New document) answers with them as one line `Warning: change
document spec-<key>: …` (the first 5; `task_context` lists all).

A card is **open** here while it is not done and not cancelled (an archived card is done). For
every open card with a change document, the sections its targets name, and the sections linked to
the card with `task_doc_link`, are **changing**: the document page shows under each such heading
one line per card, `changing on <stage>: <key> · spec-<key>` (the key links the card, the name its
change document); `doc_get` of such a section adds `<!-- changing on <stage>: <key> ·
relaybay://doc/<project>/spec-<key> -->` per card right under its header comment (the current
version only); `doc_outline` ends such a heading's line with ` · changing: <key>, …`. A target
that names no section marks nothing. The marks of a document are worked out once per answer: one
read each of the project's documents, its open cards and its linked sections, then each change
document's text.

`task_context` of a card with a change document has a `## Change document` section: the document
with its version, `doc_get` call and board link; a line `- <doc>#<anchor> · <op>` per target and
op; `- problem: …` per problem; and `Other cards changing the same sections: <key> (<stage>)
<doc>#<anchor>, …` when another open card changes one of the same sections (by its targets or
linked sections). Moving a card to done (any stage move: `task_set_stage`, `task_update`, the
board) adds hints, no rule warnings: `Hint: <key> is done but its change document spec-<key> is
still there: merge it into the spec and delete it: doc_update(…, delete=true).` while the
document exists, and `Hint: <key> is done but its deploy notes are not carried out: …` while
`deploy_notes` is set and `deploy_done` is not ([§1](#spec-task-deploy)). A cancelled card gets
neither.

<a id="spec-documents-links"></a>

**Links to sections.** `relaybay://doc/<project>/<doc>` with an optional `#<anchor>`, in a task's
description, progress, reports and comments, becomes a link to `/p/<project>/docs/<doc>#<anchor>` when
the board renders them, written as plain text or as a markdown link; inside a code span it stays code.
An anchor compares without case; a missing section links to the document and gets the mark
`no such section`, a missing document stays text with `no such document`. Only this scheme is touched, the
renderer keeps raw HTML off, and the target comes from the slug and anchor characters the pattern
allows. Links are resolved on render: one read per linked document per page, whatever the number of
links. A task whose document field is exactly such a link shows it as a link in the task dialog and a
collapsible panel with the document (or the section; the whole document when the section is missing)
and a link to its history. A field holding an absolute local path that a file document of the
task's project reads (its `source_path`, compared with separators and, on Windows, case folded;
when that finds none, with the field's links and junctions resolved, never for a network path)
counts as a link to that document; any other value of the field is
shown as text, never fetched or opened. A heading reached by an anchor stops clear of the sticky
top bar, and signing in keeps the `#anchor` the link had.

<a id="spec-memory-decisions"></a>

**Decisions** carry `scope` (`task` when a task is set, else `project`), a source (`report`,
`event` or `comment` with its id, which must exist in the project, or `message` with an external
id, plus display text; or display text alone), `valid_from` / `valid_to` (when the fact held) and
`recorded_at` / `superseded_at` / `superseded_by` (when it was recorded or replaced; for decisions
from before 0.3.0 `recorded_at` and `valid_from` are their creation time). `supersedes` on
`decision_add` marks a current decision of the same project as replaced (it gets `superseded_at`,
`superseded_by` and `valid_to`); nothing is deleted. A decision that is already superseded cannot
be superseded again (the error names the newer one), so chains have no cycles. `decision.project_id`
stays required: facts for all projects are global memory notes. `area` (migration 0055) names the
part of the project a decision is about, by area name, cleaned like a card's area but not added to
the project's areas; `decision_list` shows it, and `task_context` lists a project decision on the
cards of that area ([§ task context](#spec-task-subject)).

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
decisions, notes, reports, comments and backlog items (title; body, location, source in words,
triage note and decline reason as the body; [§12](#spec-backlog-model)). The service creates it at start when SQLite has FTS5 and
keeps it in step in the same transaction as each write; at start it is rebuilt when its row count
differs from the items it covers (a new index, an upgrade). Query words become quoted prefix terms
OR'ed with their Snowball stem (English, or Russian for words with Cyrillic letters), all words
required; ranking is bm25 with a title hit weighing most. Without FTS5 (the table cannot be
created) nothing fails: search runs as `LIKE` substring matches of each word or its stem over the
same columns (case-insensitive for ASCII letters only). A hit names its line: the line holding the
most query words. Search with a project covers its items plus global and role notes; superseded
notes and decisions are left out unless asked for.

<a id="spec-memory-page"></a>

**Docs & rules page** (`/p/<slug>/docs`): a search box over the index; the project's derived pulse
line and its pulse note with the note's age ([below](#spec-digest-pulse)); the project's documents
grouped by kind, the well-known kinds first, then the others by name (title, name, version, last
change, "lives in the repository", "file missing since …" for a file document whose file is gone),
and **New document** ([above](#spec-documents-edit)); decisions (current, superseded ones collapsed with
"superseded by #n"); memory notes of the project plus global and role notes (superseded ones
collapsed); blocks of the project and global ones, each with an edit form for the owner (text,
description, limit, owner-only; CSRF and the board cookie like other forms, a refused write keeps
the typed text); the effective rules (`rules_get` rendered). Documents, decisions and notes
refresh live on `document.updated`, `document.deleted`, `decision.added`, `memory.*`, and the pulse on
`project.pulse_set`; blocks and search results stay as
the owner left them. A document page renders the markdown safely with heading anchors (an explicit
anchor's line is not shown, its name leads to the heading), a `changing on <stage>: <key> ·
spec-<key>` line under each heading an open card changes ([§8](#spec-change-documents)), a table of contents next to the text
that stays in view (headings of levels 1 and 2, deeper ones folded under theirs, each with its
size, the section being read marked), a search box (`?q=`: `doc_search` in this document, one row
per section, a click goes to the section with the words marked), the history with a link per
version and a diff between any two versions (escaped lines, added and removed lines marked). A file
document shows where it lives, when it was read and changed, its hash and masked lines, no edit
links, and the changes seen in the file instead of versions. A search hit on Docs & rules links to
its section.

<a id="spec-digest"></a>

**Digests** are assembled by relaybay itself, with no model: markdown index lines, bodies on demand.
Each section ends with the tool that shows more. Tools: [MCP tools](mcp-tools.md#digests).

<a id="spec-digest-pulse"></a>

**Pulse.** The pulse line is derived from the cards, with no model: cards moved to another stage
and reports written in the last 7 days, how many open cards wait for the owner (`owner_names`), and
the first line of the project's newest report with its card and age ("last 7 days: 3 cards moved,
12 reports · 1 waits for the owner · latest: acme-shop-4, 2h ago: verified · M · checkout"). It is
shown on All boards, on Docs & rules and in `portfolio_digest` and `project_digest`.
`project_pulse_set(project, text)` writes an optional hand-written **note** on the project row
(`pulse`, `pulse_at`, `pulse_by`): at most 5 lines and 600 characters (over that a `ValidationError`
names both counts), secret scan like other memory writes, event `project.pulse_set`. The note is
shown under the pulse line with its date and author; it has no stale mark, and nothing asks for
it (a stage move adds no `Note:` line). `pulse_stale_days` is accepted and ignored.

<a id="spec-digest-levels"></a>

**Levels.** In priority order within each digest (the first section is identity and state):

| Digest | Sections, most important first |
|---|---|
| `portfolio_digest` | identity and counts (the count waiting for the owner is the Inbox badge); one line per project (stage counts, the derived pulse line, the note's first line with its age); what waits for the owner (the Inbox's rows: open questions and approval requests first, then each open card once with its marks, proposals and notices, then the grouped lines); **Open tasks**: one line per open card of a project not archived that is not listed there nor as stale, by project, work in progress first (`[stage] key title · note: stage note · waiting on · next · PR #n · doc <document field>`); current decisions of the last 7 days; open tasks with no change for `task_stale_days` (default 5); changes; global blocks |
| `project_digest` | identity, stage counts, description, the derived pulse line and the note; open questions (tasks waiting on the owner, `owner_names`); open tasks with next step and waiting on, work in progress before the backlog (`dev`, `review`, `test`, `live`, `plan`); the merge order of the test cards with its warnings when two or more are in test ([§1](#spec-merge-order)); recent current decisions; changes; project blocks; current project and global notes, those flagged `[verify]` (past `review_at`) or `[never read]` first |
| `task_context` | identity, fields, description and progress (with a `Blocks:` line); plan with the current step (the plan reminder's line) and the steps; other bound tasks (session start only); the cards blocking it (`## Blocked by`, [§1](#spec-task-blockers)); unread owner comments; the task's current decisions and the project's related to the card; changes; linked document sections; the card's change document (`## Change document`, [§8](#spec-change-documents)); the effective rules line; relevant notes; open backlog items of the card |

**Linked document sections:** first the sections linked to the card with `task_doc_link` (or
`task_create(sections)`), in the order they were linked; then the task's document field when it is
a `relaybay://doc/...` link (or the file of a file document, above), then any `relaybay://doc/<project>/<doc>#<anchor>` in the description
or progress, each once. Each becomes a
pointer without the document's text: `spec#gift-cards "Gift cards" · lines 2140-2610 · 52k chars
(~15k tokens) · shop:docs/spec.md · doc_get("shop", "spec", section="gift-cards")` (the file only
for a file document, the anchor as the card wrote it, an explicit anchor included). A link without
an anchor names the document's size and `doc_outline`. One that does not resolve is listed as
`broken`. At most 5 links; the rest are counted.

<a id="spec-task-sections"></a>

**Sections linked to a card** (`task_doc_section`, migration 0055). `task_doc_link(task, doc,
section, remove?)` links a section of a document of the task's project: the anchor (a heading
anchor or an explicit anchor, `#` and case do not matter) is checked against the document's
current outline (a file document's index is brought up to its file first, as `doc_outline` does);
an unknown one is refused with the known anchors, an unknown document with the project's
documents. The link keeps one name per section, whichever name it was made with: the
section's explicit anchor when it has one (it outlives a reworded heading), else its heading
anchor; linking it again by its other name changes nothing, and removing it by either name
unlinks it (a section that is gone, by the name it was linked with). A section that disappears later stays
linked and shows as `broken` (in `task_context`, `task_get` and on the card), and can still be
removed. A link made twice, or a removal of a link that is not there, changes nothing and says
so. A change is a `task.updated` event with `changes.sections` (`added` or `removed`:
`["doc#anchor"]`); the task's version stays. The answer is the section's pointer line.
`task_create(sections)` takes a list of `doc#anchor` (or `relaybay://doc/<project>/<doc>#<anchor>`
of the same project), checked the same way before the card is created (one bad entry creates
nothing); `task.created` carries `sections`. `task_get` lists them under "Linked document
sections"; the card page under "Sections" in the task details, with the section's title and the
`broken` mark. Deleting the card or the document deletes the link.

<a id="spec-task-subject"></a>

**The card's subject:** the stems (the `memory_search` stemmer) of the words of four letters and
more, without bare numbers and common English and Russian words, of the card's area name, the
titles of its linked sections (those still in the outline) and their heading paths below the
document's title (the document's first top-level heading names the document, not the subject;
a section that is that heading adds its own title), and its title. Common words are compared by
stem, so their other forms are left out too. A linked section's **number** is the number its
heading starts with (`9.6a` of `9.6a Payout provider`).

**A text names a linked section** by its anchor or its number standing alone (not part of a
longer word, anchor or number). A one-word anchor (no dash) counts only as `doc#anchor`
(`spec#payouts`, a `relaybay://doc` link too), and a bare number (no dot, no letter) only after
`§`, `section` or `sec.` (`§3`, `section 3`): alone they are ordinary words, and the subject's
stems already match words.

**Open backlog items** in `task_context`: items with status `new`, `later` or `planned` of the
card's project that are linked to the card (`task`), or of the card's own area (the same name,
case does not matter; a word shared with the card's title is not enough), or that point inside a linked section: a `path:line` in the location, title or text whose line is in
the section's lines (with the subsections) of the same file (a file document; the path matches by
its file name, or by its end when it names folders; separators and case do not matter, leading
`./` and `../` are dropped, and an absolute path with a drive letter such as `C:\work\docs\spec.md:5`
must be the file's own path), or text that names the section. One line each, `- #12 [payouts] Retry fails twice · bug
· priority 3` (no area, no kind: left out), linked ones first, then the highest priority, then the
newest; at most 8, the rest counted with `backlog_list(project="<slug>")`.

**Decisions** in `task_context`: "Decisions" holds the card's own current decisions (at most 10,
the rest counted with `decision_list("<slug>", task="<key>")`); "Related project decisions"
holds the project's current project-scope decisions **related** to the card (at most 10, newest
first): the decision's `area` (optional on `decision_add`) is the card's area name without regard
to case, or its text names a linked section, or its text holds at least two stems of the card's
subject. The others are not listed; the section's last line counts them: `… N project decisions
not related to this card: decision_list("<slug>")` (`1 project decision`); when the budget cuts
that line, the count joins the section's `… N more`. Decisions of other cards are never listed. What
every card must follow belongs in the project rules, which `task_context` names.

**Unread owner comments** in `task_context`: the count and the ids; the text only of comments the
existing channels already delivered to the reader's session. Nothing is marked delivered here.

**Relevant notes:** current project and global notes matching a word of the task's title, best search
match first, at most 8.

<a id="spec-digest-budget"></a>

**Budgets.** `digest_budget_chars` (default 12000, about 3k tokens); a call's `budget` is held between
500 and 40000. The output never exceeds the budget, footer included. Over budget the lowest
priority section loses whole lines from its end first, then the next one, and says `… N more: <tool>`;
only when even that does not fit is a section dropped, lowest priority first, and the footer names
it if there is room. In `task_context` the parts of design 0027 shrink first, in this order: the
open backlog items, the related project decisions with the count of the unrelated ones, the
linked document sections after the first;
then the older sections by their priority, the first linked section and the card's own decisions
among them. The first section goes last and is never dropped. Every value in an index line
is collapsed to one line and clipped. A budget cut in the changes holds the reader's cursor back
([below](#spec-digest-cursors)).

<a id="spec-digest-cursors"></a>

**Reader cursors.** The reader is the calling session: its desktop id, else its name
(`reader_cursor`: reader, level `portfolio` / `project` / `task`, `ref_id` the project or task id, 0
for the portfolio, `last_event_id`, `read_at`). A digest shows the events after the reader's cursor
for that level and ref and moves the cursor in the same call, but only when every change group
was rendered: if the budget cut lines of the changes or dropped the section, the cursor stays, the
next read still has them, and the footer says the position was not moved. The 30-line cap below is
no such cut. With `since_event` it shows from there and moves nothing. Without a reader, and on a
reader's first read, it shows the last 24 hours. A read that finds nothing new after the cursor
leaves it and needs no write: the next read shows the same either way, so `read_at` is when the
cursor last moved.

Changes are grouped per task (or per project, or `global`, outside any task), one line with the
kinds counted, newest first, at most 30 lines, then `… N more: events_since(since_id=…)`
(`project="<slug>"` included in a project or task digest). Left out:
`session.status_changed`, `session.seen`, `session.todo_updated`, `comment.delivered`,
`comment.read`, `plan.acceptance_delivered`. `task_context` also leaves out the caller's own `live.action` events, which its
hooks wrote. The footer names the event id reached.

<a id="spec-digest-session-start"></a>

**Session start.** The `SessionStart` answer (every source: startup, clear, compact, resume) adds to
`additionalContext`, after the hook session line:

- an exactly linked session bound to an open task: a first line `relaybay card context for <key>:
  the same as task_context, so do not call it now; call it later only to refresh.`, then
  `task_context` of its most recently bound task, the other bound open tasks as one index line each. The plan reminder is not sent as well when the
  budget left its lines in that text; otherwise it goes out after it;
- any other session whose `cwd` maps to one project: `project_digest` limited to
  `session_context_project_chars` (default 1500), which holds no comment text. Only an exactly linked
  session is a reader there: a guessed row gets no cursor, and its desktop id is not printed;
- otherwise nothing.

A nested `claude` (the desktop session id inherited, attended field explicitly `"0"`) gets only the
hook session line: no context, no reminder, no cursor row. It never links to the desktop session,
by host id or by guess, whatever that row's state; an empty or missing field is treated as before,
because older desktop apps may not send it.

After `startup` and `clear` the agent remembers nothing, so it is not told "since your last read":
the changes cover the last 24 hours, or from the cursor if that is older ("since this session last
read"). `compact` and `resume` keep the cursor rule.

The total stays below the 8000-character limit of a hook answer, with 500 characters left free. This
moves an exactly linked session's cursor. Any exception there is logged and the answer goes out as it would
without it, plan reminder included. `session_context = "off"` turns all of it off.

<a id="spec-import"></a>

### Markdown import and export

`relaybay import <root>` and `relaybay export <root>` map a folder of markdown files to documents,
decisions, notes and tasks and back. Guide with the layout and examples:
[Import and export a markdown setup](how-to/markdown-import-export.md).

<a id="spec-import-mapping"></a>

**Mapping.** `projects/<slug>.md`: the project (created if missing) and document `brief`; bullets
under a `## Decisions` heading become project decisions with the source `message:import <path>`.
`projects/<slug>/docs/<doc>.md`: document `<doc>`, its kind the name when that is a well-known kind
([above](#spec-documents-kinds)), else `doc`. `TASKS.md`: one task per row of every table with a project and a task column, with
the columns stage, waiting on, next step, document and ref. The leading stage word (up to three words
are tried) goes through an alias map and the rest, without one leading separator, is the stage note;
an unknown word gives stage `plan`, the whole text as the note and a warning. `--aliases <file>` adds
column, stage and heading names. An optional `rank` (or `order`) column sets the card's place in its
stage column ([§2](#spec-board-order)): a whole number from -1000000000 to 1000000000, else an `error`
row; an empty cell or a registry without the column keeps the board's order, and the rank then
counts neither for "changed on the board" nor for a file that says the same. An optional `area`
column carries the task's area by name ([§1](#spec-areas)): matched without regard to case, a new one
added to the project; an empty cell or no column keeps the board's area. A text over 32 characters,
or a new name past the project's limit of 24 (counting the areas the same import adds before it),
leaves the task without it and adds a warning: the dry run says what the apply does. An area counts for "changed on the board" only on a task that has one. Export writes
the column only when a task has an area, so a board without areas exports the file it did before
them. `projects/<slug>/tasks/<ref>/TASK.md`: the task's description (the
`## Task` section, else the first paragraph), its `## Decisions` bullets as task decisions, the whole
file as document `task-<ref>`, linked as `relaybay://doc/<project>/task-<ref>`; a folder with no row
creates the task in stage `plan`. `memory/<name>.md` and `projects/<slug>/memory/<name>.md`: a global
(or, with `metadata.role`, role) note and a project note: title from `name`, else a first `# Heading`
(which then leaves the body), else the file name; kind from `metadata.type`, anything unknown is
`reference`; `description` as the first line of the body unless it is already; `[[links]]` in `links`.
`retro.md`: document `retro` of `--retro-project`, skipped with a warning without it. `backlog.md`:
backlog items ([§12](#spec-backlog-import)), exported back the same way.
`projects/<slug>/code.md`: the project's repositories (a table with name, kind, path, in order) and
its tasks' links (a table with task, repos, prs: the task by its registry ref, repository names
comma-separated, pull request entries one per line as `prs` takes them, [§1](#spec-task-code));
export writes it for a project with repositories or links. Import adds a repository missing on the
board (in the file's order, after the board's), keeps one that says the same, and calls one with
another path or kind a conflict unless `--overwrite`; a task's links are only ever added, never
removed, and a row whose task is not in the registry is an error line. A row's task is
the one imported for its ref, else one no other item is imported for: the one whose key is its ref, or,
for a row with no `ref` cell whose title no other such row claims, the only one with its title.

<a id="spec-import-plan"></a>

**Plan.** A dry run is the default: it plans on a throwaway copy of the database (SQLite backup API),
so neither a migration nor an index rebuild changes the real one (SQLite may leave its empty `-shm` and
`-wal` files next to it) and a missing database stays missing.
It lists per kind what would be added, updated, unchanged, skipped, in conflict, refused or in error,
with a unified diff for documents and notes (at most 40 lines per item, 400 in all), and a plan hash
over kind, item, source, action and the hashes of file and target. `--apply` plans again on the real
database; with `--expect <hash>` the hash is checked on a copy first, so a different one writes and
migrates nothing and exits 1. Exit code: 0, or 1 when
anything was refused, in conflict or failed; 2 for a bad root, aliases file or arguments.

<a id="spec-import-idempotent"></a>

**Idempotence.** `import_item` holds one row per item: kind, item name, source path relative to the
root, target id, the file's value hash and the target's value hash right after the import. A re-run
with no changes writes nothing and no events. A target already saying what the file says records
bookkeeping only, without an event. A changed file updates through the service: a new document
version with the reason `import`, a note update, task fields only where the file has a value. All
writes go through `Service` one item at a time with the actor `import`; an error in one item does not
stop the others. It runs next to `relaybay serve` on the same data folder (WAL, busy timeout); the
board shows the items after a refresh: the events are in the log, but this process cannot push them
to the server's live stream.

<a id="spec-import-conflict"></a>

**Conflicts.** A target changed since its last import, or on the board without an import, is listed as
`conflict` and skipped, unless the file says the same; `--overwrite` takes the file's text (a new
version, old versions stay). A task's description is an exception, because export cannot write it
back: only a `TASK.md` whose description changed since the import can conflict. Decisions are
append-only: a new bullet adds a decision; one that shares at least 60% of its text with a bullet gone
from the file adds a decision superseding the old one; a bullet gone without a successor leaves its
decision current, and a decision superseded on the board is not added again (both `skipped`).

<a id="spec-import-refusals"></a>

**Refusals.** An item with a secret value is `refused`, naming the file, line and detector, never the
value, with the detectors and allowances of the effective policy ([§7](#spec-policy)). Every path and
all cells of a registry row are scanned before anything is named from them: a refused row is shown as
`TASKS.md:<line>` and a file name holding a secret as `(hidden name)`; a task folder of a refused row
is skipped with it. Parsed note fields are scanned too, so JSON escapes hide nothing. Only the root's
top level, `projects/` and `memory/` are read, without following a link out of the root (a link inside
is read at its real place). A file over 2 MB, a document, note or description over its limit, and a
name that is no valid slug are refused; a file that is not UTF-8 is an error for that file. Other
markdown files there are `skipped`, also `memory/MEMORY.md`, an index.

<a id="spec-import-export"></a>

**Export.** `relaybay export <root>` writes documents (to the file they were imported from, else their
place in the layout; a task document no task links to under `docs/`; a document that lives in a
repository only when it has text), current and superseded decisions
to `projects/<slug>/decisions.md`, current notes and the registry with a `ref` and a `rank` column. It reads a copy
of the database. The same data gives the same bytes: sorted, no times but stored ones; an import of an
export changes nothing. A document's file comes back byte for byte: `import_item` keeps what the
import's normalisation removed (the line ending `lf` or `crlf`, a UTF-8 byte order mark, the exact
whitespace before and after the stored text), and export puts it back, so a file imported and not
changed on the board is written with the same SHA-256. A file with mixed line endings imports with
a warning and exports with LF; only a changed line ending or outer whitespace updates the
bookkeeping, nothing on the board. Dry run by default, `--apply` writes. The manifest `.relaybay-export.json`
holds each file's hash and whether export wrote it. One rule for overwriting: a file export did not
write (`written: false`, or not in the manifest) is a `conflict` whatever its hash and `--force` takes
it over; a file export wrote is overwritten only while it matches its hash, else it is a `conflict`
until `--force`. A file that says the same in its own words (the same note with its tags, or a registry
row with the document field an import would derive) is kept; the manifest takes its new hash only when
the difference is line endings or outer whitespace, so a hand edit stays protected. A file export wrote
that the board no longer has is removed, unless changed by hand; files it did not write are never
removed, and a manifest entry that is no path of the layout is `refused`, also with `--force`. Two items
for one path (compared without case): a later note takes a `-<id>` suffix, a later document its `docs/`
path, otherwise an `error` and exit 1. In a diff, a file on disk holding a secret shows `diff hidden:
line N (detector)`. Export refuses to run without a database and, with `--apply`, from a board with no
projects when the manifest is not empty. A write through a link out of the root is refused, a manifest
that cannot be read stops the run. Import does not read `decisions.md`.

<a id="spec-security"></a>

## 9. Security model

relaybay is a single-user local server. The security layers, in request order:

<a id="spec-security-usage"></a>

What Activity keeps of a desktop `SendMessage`: sender, recipient and time, never its text. Nothing
of this leaves the machine.

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
   others logs a warning). Required as `Authorization: Bearer` on `/mcp`, `/hooks` and `/api`
   ([notifications](#spec-desktop-notify)); the board's cookie is not enough there.
   `relaybay token --rotate` replaces it (restart the server).
   `/healthz` is public and returns `status`, `version`, `token_id` (a 12-hex-digit HMAC
   fingerprint that reveals nothing about the token) and `rules_hash` (the rules in the config the
   server started with, not the current version, [§6](#spec-rules-hash)).
5. **Browser session.** `/login?token=…` (or the sign-in form) sets an `HttpOnly`, `SameSite=Strict`
   cookie (`Secure` over https, one year) derived from the token and redirects, so the token leaves
   the address bar. Browsers share cookies between the ports of one host, so every cookie name of the
   board carries the port of the request's `Host` (`relaybay_session_8765`,
   `relaybay_login_csrf_8765`, `relaybay_login_error_8765`, `relaybay_flash_8765`; no port there, the
   plain name) and a server beside another one does not sign you out of it; the access log redacts the `/login` query. Unauthenticated page visits redirect
   to `/login?next=…` (only local paths are accepted as `next`). A Bearer header also works for the
   board's URLs, except the board's own writes (project settings, policy, rules; [§2](#spec-board-repos)).
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
    `GET /files/<id>/annotated` answers the same way with `image/png`; its upload and the viewer's
    other writes need the board cookie and the CSRF header ([Annotations](#spec-annotations)).
11. **Opening a terminal** ([§2](#spec-board-state)): the only place the board starts a program.
    A board write (cookie, CSRF, no Bearer) from a loopback client asking for a loopback `Host`
    only (a board published through a proxy does not open terminals); it takes a session's
    internal id and nothing else, runs the fixed `claude --resume <uuid from the database>` in the
    session's start folder (which must exist) as an argument list, never through a shell string,
    without Claude Code's own environment variables, at most once until the session starts, and
    records `session.terminal_opened`.
12. **Backups and updates:** a backup before a new version copies the database, `config.toml`,
    `policy.json` and the `token` into `backups/` of the data folder (created 0700 on POSIX), so
    it is as sensitive as the data folder itself ([§13](#spec-backups)). An installer is run only
    after its SHA-256 matches `SHA256SUMS.txt` of the same release: that checks integrity, not
    authenticity, since nothing is signed; downloads come only from the project's GitHub release
    URLs over HTTPS ([§13](#spec-updates)).

<a id="spec-security-forms"></a>

**Full-page forms** (project Settings, Settings → Policy, Settings → Integrations without htmx)
answer a successful write with `303` to their page (POST → redirect → GET), so a refresh does not
send the form again; the outcome ("Repository saved.", "Saved the acme-shop policy.", the check
results) rides in a short-lived `HttpOnly`, `SameSite=Strict` cookie (`relaybay_flash_<port>`, 60 s) that
the page shows once and deletes. A refused write still renders the page with the reason, and the
"confirm raising live" step is a rendered page whose confirmed save redirects.

<a id="spec-no-telemetry"></a>

**No telemetry.** relaybay sends nothing about you or your board anywhere: no analytics, no CDN.
The one outbound call is the update check ([§13](#spec-updates)): an unauthenticated `GET` of the
latest GitHub release with `User-Agent: relaybay/<version>` and nothing else, at most once per
`update_check_hours` (default 24) by the server and once by the desktop program;
`update_check = false` in `config.toml` (and in `desktop.toml` for the program) turns it off. The
program downloads an installer only on a click of Update. The other network clients in the code
talk to relaybay's own server (`relaybay token`, `relaybay setup --check`, the command hook), and
they bypass system proxies for a loopback address.

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
optional global policy `policy.json` ([§7](#spec-policy)), `attachments/` (screenshots,
[§1](#spec-attachments)), `last_version` and `backups/` (the backup before a new version,
[§13](#spec-backups)). An invalid value stops the start with a
message naming the key (an invalid policy file: its path and the problem). Keys, variables and
flags: [configuration reference](reference/configuration.md); policy keys:
[policy reference](reference/policy.md).

<a id="spec-distribution"></a>

The data directory itself is `--home`, else `RELAYBAY_HOME`, else `data_dir` in `desktop.toml`
([§13](#spec-desktop-settings)), else the per-user data dir; `relaybay doctor` names which one.

`max_running_turns`, `min_free_memory_mb` and `claude_path` set runs inside the program up to
0.11, `usage_enabled` and `usage_interval_seconds` the usage reader. Since 0.12 ([§1](#spec-run),
[§2](#spec-usage)) they are accepted in `config.toml` and ignored, so an older file
still starts the server, and their `RELAYBAY_*` environment variables are no longer read;
`relaybay doctor` names the ones found under `ignored settings`.

**Distribution.** The version lives only in `pyproject.toml`; `relaybay --version`, `/healthz` and
the MCP server info read it from the installed package (the frozen program carries the package
metadata). The wheel and sdist carry the templates,
static assets and migrations; `python -m relaybay` is the same as `relaybay`. The Docker image
(`ghcr.io/codenator81/relaybay`, `linux/amd64` and `linux/arm64`) runs `relaybay serve` as a
non-root user with `RELAYBAY_HOME=/data` (a volume), `RELAYBAY_HOST=0.0.0.0`, port 8765 and a
`/healthz` health check, and sets `RELAYBAY_DISTRIBUTION=docker` so it knows its install kind. The
desktop program ships as installers ([§13](#spec-desktop-install)) built from one frozen folder with
three executables: `relaybay` (the command line), `relaybay-desktop` (the program) and
`relaybay-hook` (the hook forwarder, standard library only).

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
| `0014_digests` | `project.pulse`, `project.pulse_at`, `project.pulse_by`; `reader_cursor` (reader, level, ref id with 0 for the portfolio, last event id, read at; primary key reader + level + ref id). Additive only |
| `0015_import` | `import_item` (kind, item, source path, target id, content hash, target hash, imported at; primary key kind + item). Additive only |
| `0016_annotations` | `annotation` (screenshot, kind, colour, stroke width, geometry JSON, pin number unique per screenshot, comment, created by / at, updated at, deleted at), `annotation_reply` (annotation, author kind and name, session, body, created at), `attachment.annotated_name`, `annotated_size`, `annotated_sha256`, `annotated_sent_sha256`. Additive only |
| `0017_usage` | `session.transcript_path`; `usage_file` (path, session, parent file for a subagent's, read offset, skipping, unknown lines, error, created at, read at), `usage_day` (file, day, session, model, input, output, cache read, cache write, messages; primary key file + day + model), `usage_message` (message id, file). Additive only |
| `0018_rule_versions` | `rule_set` (display scope, project, current version, config hash), `rule_version` (set, number, body, rendered markdown, author, session, reason, diff, open notice, created at), `rule_proposal` (scope, project, patch, reason, proposed by, session, status `open` / `accepted` / `rejected`, version made, created / decided at, decided by), `rule_seen` (session, project or 0 for global, version). Additive only; the first start fills them ([§6](#spec-rules-versions)) |
| `0019_saved_filters` | `saved_filter` (project, name unique per project, stage, waiting on, session, text, created at). Additive only |
| `0020_document_source` | `document.source_url` (a document that lives in a repository, [§8](#spec-documents-repo)). Additive only |
| `0021_intake` | `intake_source` (slug, kind, name, `may_wake`, created at, last message at), `intake_message` (source, external id unique per source, thread, author, text, received at, status, refused reason, task, outcome), `intake_thread` (source + thread → task), `backlog_item` ([§12](#spec-backlog)), `task.autonomy`, `task.answer_received_at`, `task.answer_message_id`, `project.autonomy`; the `comment` table rebuilt to allow the author kind `external` and `import_item` to allow the kind `backlog` (rows and ids kept). No data changes |
| `0022_plan_step_added` | `plan_step.added_after_acceptance_at`, `added_by`, `added_reason` (a step added to an accepted plan, [§1](#spec-plan-living)). Additive only |
| `0023_backlog_done` | `backlog_item` rebuilt to allow status `done`, plus `link` ([§12](#spec-backlog-model)). Existing rows keep every value |
| `0024_backlog_queue` | `backlog_item.priority` (default 0), `assignee`, an index on project, status, priority and id ([§12](#spec-backlog-model)). Additive only |
| `0025_session_no_live` | `session.no_live_at`, `session.no_live_by` (the owner's switch, [§7](#spec-no-live)). Additive only |
| `0026_brake_budgets` | `brake` (scope, project, session, task, reason, source, budget, created and released at and by), `token_budget` (one row per target), `budget_mark` (budget, period key, level), `usage_task_day` (file, task, day, model and the numbers). Additive only, no backfill |
| `0027_session_messages` | `session_message` (sender session, name and whether a hook proved it; recipient session and name; task; body; `in_reply_to`, depth; created, delivered and read at, whether a hook proved who read; Stop blocks; loop stopped and seen at). Additive only |
| `0028_task_rank` | `task.rank` (default 0) and an index on project, stage and rank; existing tasks ranked 1..n per project and stage in the order the board showed (`priority` desc, `updated_at` desc, `id` desc), so no card moves ([§2](#spec-board-order)). Additive only |
| `0029_project_repos` | `project_repo` (project, name unique per project, path, kind, position, created / updated at), `task_repo` (task, repository, added by, how `auto` / `agent` / `owner`, added at; unique per task and repository), `task_pr` (task, optional repository, url unique per task, number, title, added by / at), an index on open `task_session` rows by session; seeded one repository per project with a `repo_path` ([§1](#spec-project-repos)). Additive only |
| `0030_project_run_defaults` | `project_run_defaults` (project, folder, model, effort, permission mode, allowed tools, chrome, updated at / by), `app_setting` (key, value, updated at / by; `run_login_any`). Additive only, no backfill: a project without a row has the defaults |
| `0031_run` | `run` (task, project, its session, cwd, cwd repository, model, effort, permission mode, allowed tools, chrome, Claude Code session id, status with all seven values, reason, sign-in method, where hooks and MCP come from, started by and its kind, created / updated / ended at, ended by); one open run per task and one run per Claude Code session (unique indexes). Additive only |
| `0032_run_turn` | `run_turn` (run, sequence, text, sent by and its kind, queued / started / ended at, pid, outcome, result subtype, exit code, number of turns, duration, four token counts, denials and denied tool names); a unique index allows one running turn per run. Additive only |
| `0033_run_worktree` | `run_worktree` (run, project repository, repository name, source path, path, path key, branch, created by the run, the copied file names, created / removed at); an index per run and one on the path key of the not removed. One open run per path is checked by the service. Additive only |
| `0034_run_with_owner` | `run.with_owner_at`, `run.with_owner_by` (the run is with the owner in the Claude desktop app: no turn starts), `run.desktop_opened_at`. Columns, not a status, so the status CHECK of 0031 stays. Additive only |
| `0035_run_event` | `run_event` (turn, seq unique per turn, type, subtype, `parent_tool_use_id`, read at, length, truncated, the masked line compressed with zlib); `run_turn.report_id` (the report the turn's result became). Additive only |
| `0036_report_check` | `report_check` (report, line, `where` as `location`, claim); an index on report and line ([§1](#spec-report-marks)). Additive only, no backfill: older reports have no marks |
| `0037_question` | `question` (task, run or session, asked by, kind `question` / `look`, text, options JSON, default, blocking, look URL and account, `default_after`, asked at, status `open` / `answered` / `defaulted` / `withdrawn`, answer, answered by `owner` / `default`, answered / defaulted / withdrawn at, withdrawn by, the turn or owner comment that carries the answer, told / delivered at); checks that a look question blocks and has its URL and that a blocking one has no deadline; indexes per task, run, comment and on the open ones by deadline ([§1](#spec-questions)). Additive only |
| `0038_task_archive` | `task.archived_at`, `task.archived_by` (archived by hand), `task.unarchived_at` (the last "Back from the archive") ([§2](#spec-board-archive)). Additive only, no backfill |
| `0039_approval` | `approval` (task, run or session, requested by and via `agent` / `guard` / `denial`, action `commit` / `push` / `merge` / `deploy` / `sql`, a push's mode `plain` / `force` / `delete`, repository, branch, PR, environment, summary, rollback, requested at, status `open` / `approved` / `refused` / `used` / `expired`, decision, decided by and at, the owner's line, valid until, used at / by / session, expired at and why, the turn or owner comment that carries the decision, told / delivered at); checks that a push or commit names its branch, a merge its PR, a deploy or SQL change its environment, an approved deploy its rollback, and that the status matches the decision and the use; indexes per task, run, comment and on the open and approved ones ([§1](#spec-approvals)). Additive only |
| `0040_run_lifecycle` | `run.secret_seen_at`, `run.touched_live_at`, `run.transcript_path`, `run.deleted_at`; `project.run_retention` (days as text, or `off`; default `30`); `run_worktree.kept_reason`, `run_worktree.question_id` (the owner's question whether to remove it), `run_worktree.kept_at` (the owner's keep); an index on `run_event.ts` for the retention and one on the question of a kept worktree. Additive only |
| `0041_approval_reservation` | `approval.reserved_at`, `approval.reserved_for` (the call's `tool_use_id`), `approval.reserved_call`, `approval.reserved_session_id` (the approval held for one call until it ran), `approval.released_count` (default 0; the third release expires it); a partial index on `reserved_for` of the held ones ([§1](#spec-approvals)). Additive only |
| `0042_run_terminal` | `run.with_owner_in` (`terminal`: the run is with the owner in the board's Terminal tab; NULL: the Claude desktop app of 0034); a start hands back a run still marked `terminal`. Additive only |
| `0043_run_profile` | `run_profile` (name unique, description, permission mode with a CHECK against `bypassPermissions`, allowed and taken-away rules, rights JSON, kinds JSON, builtin, version, updated at / by), seeded with `agent`, `coordinator` and `read-only`. Additive only |
| `0044_run_profile_use` | `project_run_defaults.profile` (default `agent`); a project whose mode and allowed tools differ from `agent`'s gets a profile `agent-<slug>` with that pair and is pointed at it, so no run changes its rights; the old columns stay, unread. `run.profile` (NULL for older runs), `run.disallowed_tools`, `run.mode_set` (1 for older runs: they keep their own mode); `run_turn.profile`, `run_turn.profile_version`. Additive only |
| `0045_coordinator_run` | `run` rebuilt: `kind` (`card` for every existing run, `coordinator`, `check`), `task_id` and `project_id` nullable with CHECKs (a `card` has both, a `coordinator` neither, a `check` a project), `started_by_run` (the coordinator run that started it); a unique index allows one open coordinator run. `question` rebuilt: `task_id` nullable, CHECK that a question has a card or a run. Both with foreign keys off; every column, index and value kept. `run_turn.result_md` (the result of a turn without a card) |
| `0047_schedule` | `schedule` (`kind` `coordinator`, `check` or `reminder`; `repeat` `once` with `at`, `daily` with `time_of_day` and `weekdays_only`, `every` with `every_minutes` at least 5 and `max_times` 1 to 24; `timezone`; `project_id`, `task_id`, `profile` (a check's); `prompt`; `created_by`, `created_by_kind` `owner` or `coordinator`; `enabled`, `next_at`, `times_run`, `done_at`; CHECKs tie each form to its column and a check to a project) and `schedule_run` (`schedule_id`, `due_at`, `started_at`, `ended_at`, `outcome` `ran`, `missed`, `skipped`, `failed` or `stopped`, `reason`, `tokens`, `run_id`, `turn_id`, `report_id`, `notice` and `seen_at` for the Inbox); `schedule.deleted_at` (deleted: the row and its runs stay); a unique index allows one open run of a schedule. Additive only |
| `0048_project_area` | `project_area` (project, name, colour key, position, created at; unique on the project and the lowered name), `task.area_id` (`ON DELETE SET NULL`, indexed where set), `saved_filter.area` ([§1](#spec-areas)). Additive only: no table rebuilt, every task keeps its row with no area |
| `0046_coordinator_wake` | `coordinator_wake` (`run_id`, `turn_id`, `first_event_id`, `last_event_id`, `count`, `created_at`): each wake of the coordinator and the events it carried. `question.overdue_at` (the `question.overdue` event written). `coordinator_wake.returned_at` (a wake given back). `run_turn.compact_pre_tokens`, `compact_post_tokens` (a turn's `compact_boundary`), `context_tokens` (its last assistant message's context). `token_budget` rebuilt with foreign keys off: scope `session` and `session_id` (a CHECK ties them; one budget per session); every budget keeps its id, marks and brakes |
| `0049_remove_runs` | Data only, no table dropped: lists the worktrees runs created and did not remove for the Inbox (`app_setting` `removed_runs_worktrees`); cancels the waiting turns of runs and marks the running ones lost; ends every open run (status reason "runs inside the program were removed in 0.12", ended by `relaybay`); withdraws the open questions that have a run (the coordinator's questions without a card among them) and relaybay's own open questions about a kept worktree, with that reason; expires the open and the approved, unused approvals that have a run; stops the open schedule runs ("schedules were removed in 0.12") and disables every schedule not deleted or done; clears every task's "answer received" mark. Questions and approvals of desktop sessions stay open ([§1](#spec-run)) |
| `0050_session_surface` | Additive: `session.surface` (`desktop`, `terminal` or NULL), `session.entrypoint` (the raw `CLAUDE_CODE_ENTRYPOINT`), `session.start_cwd` (the resume folder) and `task_session.unbound_reason` (`session_end` or NULL), [§1](#spec-session-surface) |
| `0051_backlog_registry` | Additive, all nullable: `backlog_item.kind` (`bug`, `idea`, `question`, `debt`), `location`, `origin_text`, `triage_note`, `duplicate_of` (another item, `ON DELETE SET NULL`, indexed), `found_on` ([§12](#spec-backlog-model)). Existing items have no kind |
| `0052_archive_and_launch` | Additive, no backfill: `task.archived_from` (`live` or NULL, [§2](#spec-board-archive-live)) and `session.launched_by` (the starter's session id, `ON DELETE SET NULL`, [§4](#spec-hooks-launch-bind)); `task_session.unbound_reason` may now also be `handed_over`. Rollback: an older release ignores both columns, but its stage changes do not clear `archived_from`, so after a downgrade and a new upgrade a card archived from live and moved on in between may keep a stale `live` there; if the done window later archives it, Back from the archive returns it to live (move it by hand) |
| `0058_drop_run_tables` | Drops the run tables, unread and unwritten since 0.12: `run`, `run_turn`, `run_event`, `run_worktree`, `run_profile`, `project_run_defaults`, `coordinator_wake`, `schedule_run`. `question` and `approval` rebuilt with foreign keys off: every row and column kept, `run_id` and `turn_id` stay as plain numbers without their foreign keys (SQLite refuses writes into a table whose foreign key names a missing table). The backup before a new version ([§13](#spec-backups)) is taken before it runs; rollback is that backup (an older release needs the dropped tables) |
| `0059_drop_unread_tables` | Drops the rest of what 0.12 left unread and unwritten: `schedule`, `usage_file`, `usage_day`, `usage_message`, `usage_task_day`, `token_budget`, `budget_mark`, `brake`, `session_message`, `intake_source`, `intake_message`, `intake_thread`, and the columns `project.autonomy`, `project.run_retention`, `session.transcript_path`, `task.autonomy`, `task.answer_received_at`, `task.answer_message_id`. `task` and `backlog_item` rebuilt with foreign keys off (a column with a foreign key or a CHECK cannot be dropped in place): every other column, CHECK, index and row kept; `backlog_item.intake_message_id` stays as a plain number. The run columns of `question` and `approval` stay (a CHECK uses `run_id`). Backup and rollback as for 0058 |
| `0060_card_fields` | `task.deploy_notes`, `task.deploy_done` (0/1, default 0), `task.cancelled` (the reason), `task.cancelled_at`, `task_repo.worktree` ([§1](#spec-task-deploy), [§1](#spec-task-code)). Additive only |
| `0061_task_block` | `task_block` (`task_id`, `blocker_id`, both `ON DELETE CASCADE` to `task`, `created_by`, `created_at`, unique per pair) with an index on `blocker_id` ([§1](#spec-task-blockers)). Additive only |
| `0062_task_inspect_only` | `task.inspect_only` (integer, default 0): a card that only reads needs no accepted plan before dev ([rules](#spec-rules)). Additive |

What 0.12 left in the file unread was dropped by migrations 0058 and 0059 (above); only the
run columns of `question` and `approval` and `backlog_item.intake_message_id` stay, as plain
numbers. The archive by age is computed from `closed_at` / `unarchived_at`; only the hand mark is stored. Events are kept forever. Table details:
[architecture](architecture.md#data-model).

<a id="spec-intake"></a>

## 12. Backlog

**Intake: removed in 0.12** ([design 0025](design/0025-board-with-the-desktop.md)). Without a
coordinator inside the program nothing can tie a customer's message to a task (customers write no
keys, one chat holds many tasks); the coordinator session reads the chats and updates the cards
itself. Gone: posting messages from watchers, the Inbox groups Intake and Answers, the "answer
received" mark (cleared by migration 0049), source trust and task and project autonomy
(Settings → Intake), Forward to the agent, the task's Intake block, wake-ups through hooks and the
intake notifications. The `intake_*` tools were removed with MCP contract 2 ([§3](#spec-mcp-compat)) and the
intake tables were dropped by migration 0059 ([§11](#spec-data)). Backlog items whose origin was intake
stay, their origin shown as plain text.

<a id="spec-backlog"></a>

<a id="spec-backlog-model"></a>

**Item.** A finding for later, not a task and not a stage: project, title (up to 200), body (markdown,
up to 20000), area (free text, lowercased, e.g. `checkout`; not migrated into the project's
[areas](#spec-areas): its field suggests the project's areas first, then the texts backlog items use), origin `session` / `review` / `owner` (or
`intake` for an item made before 0.12) with the origin task, session or intake message, status `new` / `later` / `planned` /
`done` / `declined` with a decline reason (for `done` an optional note in the same field) and for
`done` an optional link (an `http(s)` link of up to 500 characters: where it was fixed without a
task of its own, a pull request or a commit; cleared by any other status), a priority (a whole number
from -9 to 9, default 0, higher first within a status), an assignee (who it is for, a name or a role,
up to 100), the task "To plan" created, decided at / by, created at / by. Title, body, area, assignee,
a decline reason and a link (MCP and board) are scanned for secret values.

As a registry (migration 0051, [design 0026](design/0026-project-knowledge.md)) an item also has a
kind (`bug`, `idea`, `question`, `debt`; none for items made before 0.14), a location (where in the
code: `repo:path:line`, `path:line` or a link, up to 300), the source in words (up to 200, when it
is not one of the origins; the origin is then `owner`), a triage note (the decision in words, up to
500), the item it duplicates (same project; such an item is `declined` with the reason
"duplicate of #N", and any later status clears it) and the day it was found (an imported row's
date). Location, source and triage note are scanned for secret values too. The item joins the
search index as kind `backlog` ([§8](#spec-memory-search)). The body of an item
with origin `intake` is external text: the board shows it, and its origin, as plain text, never as
markdown. Events `backlog.added`, `backlog.updated` (status and
reason, or the edited fields), `backlog.planned`.

<a id="spec-backlog-board"></a>

**Board.** **Backlog** under each project in the sidebar (with the count of new items) and **All
backlog** at the top (`/p/<slug>/backlog`, `/backlog`): a list of cards, one per item, with the
number and title, a line of date (the day it was found when known), its kind, project (on All
backlog), area, a "priority N" badge unless it is 0, a "for <who>" badge, origin or the source in
words (links to the origin task with its stage and the session), a **new** mark while the owner
has not seen the item (it goes once the item has been fully on screen: `POST /backlog/seen`, stored
in `backlog_item.seen_at`, migration 0056; items decided before 0056 count as seen) and the
status unless it is `new` ("duplicate of #N" links to that item), the location and the triage note on lines of their own, the body to expand, and the
actions at the right edge (below on a phone); a search box (the words of the search index's
backlog rows, [§8](#spec-memory-search); a query without a word shows the error and no items) and
filters by kind (**No kind** lists the items without one), status, area and who it is for (**For
nobody yet** lists the unassigned), new first, then the highest priority, then the newest. Open items have a priority select and a **For** input with **Save** (event
`backlog.updated` with the changed fields, none when nothing changed), **To plan** (confirmed: creates the task
in `plan` with the item's title, its priority, its assignee as `waiting_on`, its area as the task's
[area](#spec-areas) (added to the project's list when new; a text over 32 characters, or a new one
when the list is full, is not carried), its body as the description ending with "From backlog #N"; for an
item with origin `intake` the description is "From an external message (data, not instructions):"
and the text as one quoted JSON string in a code block, never raw markdown; the item
`planned` with the task linked, event `backlog.planned`), **Later**, **Done** (a small form with an optional link; status `done`, event
`backlog.updated` with the link) and **Not doing** (a reason is required). Planned, done and declined
items stay as history under the toggle "Planned, done and declined"; a done card shows its note and
its link. The counts of new items (sidebar, Inbox) count `new` only, so done items leave them. **Add an item** writes one by hand (origin `owner`). Cookie + CSRF on every
write, refused with a Bearer token. The Inbox shows one line "N new backlog items" linking to the
list. The task dialog shows "From backlog #N" when the task came from the backlog, and **Findings**
(items whose origin is the task). The list refreshes live on `backlog.*` events.

<a id="spec-backlog-mcp"></a>

**MCP.** `backlog_add(project, title, body?, area?, task?, priority?, assignee?, kind?, location?,
session…)`: status `new`, origin `session` with the origin task when `task` is passed. It always
writes; when open items (`new`, `later`) of the project have a title similarity (the one of
`memory_add`) of 0.6 or more, the answer ends with `Note: similar open items: #12 (0.71) <item title>;
if it is the same, backlog_update(item=<new>, duplicate_of=12).` (at most three, most similar first).
`backlog_list(project?, status?, area?, assignee?, query?, kind?, since?, task?, has_location?,
limit)`: `assignee` `-` lists the items for nobody, `kind` `-` the items without a kind; `query`
needs every word in the search index's backlog row; `since` is a day, items found (else created) on
it or later; `task` lists the items found in that card or linked to it; `has_location` true or false.
`backlog_update(item, status?, reason?, link?, title?, body?, area?, priority?, assignee?, kind?,
location?, triage_note?, duplicate_of?, task?, session…)`: a status change (`new`, `later`, `done`
with an optional `link` and `reason` as a note, `declined` with a reason; `link` with another status
is refused), the triage note, `duplicate_of` (closes the item as a duplicate of another item of the
same project, not itself and not a declined item; the items that were duplicates of this one
now point to the same original, with the same reason and a `backlog.updated` event each, and the
answer names them) and `task` (links an
existing card of the same project: the item becomes `planned` with that task, event
`backlog.planned`, no card is created) only for the coordinator sessions named in the config (the
`rules_update` check, [§6](#spec-rules-versions)); one of status, `duplicate_of` and `task` per call,
none of them on a planned item. Anyone else may edit title, body, area, priority, assignee, kind and
location of an item that is still new and that the same session added. `planned` is never a status
over MCP: **To plan** on the board creates a task, `task` links one. `backlog_patch(item, fields,
session…)` takes the same fields as one JSON object under the same rules (an unknown field is
refused), for a client whose `backlog_update` schema is older than the server; `-`, an empty string
or null clears body, area, assignee, kind, location and triage note and resets priority to 0.

<a id="spec-backlog-import"></a>

**Import and export.** `backlog.md` at the root, or a registry under any name with
`relaybay import --backlog <file>` (then the folder is optional, and the file stands in for the
folder's `backlog.md`; its name is checked for secret values like the folder's names): one table
with the columns date, project, area, what, origin, decision (`planned`, `later`, `done`,
`done: <link>`, `declined: <reason>`, empty for new; a done item's note is not in the file, and an
import that updates a done row keeps it), priority and assignee (also read as `prio`, `for`,
`assigned`), kind, location (also `place`, `where`, `code`, `place in code`) and note (the triage
note; also `notes`, `triage`). A file without one of the last five columns keeps the stored values,
and an old file exports as unchanged while they are at their defaults; an empty cell under such a
column resets the value (a note still takes a decision outside the vocabulary);
a priority that is not a whole number from -9 to 9, or a kind outside the four, is an `error` line.
Other column names through `--aliases` under `backlog`, the leading words of a decision under
`decisions` (to `new`, `later`, `planned`, `done`, `declined`: a declined one takes the rest of the
cell as its reason, a done one a link, the others keep the cell as the triage note). A registry's
rows are taken as written: a what over 200 characters is the body, whole, and the title its first
sentence (of 20 characters or more) when that fits 120 characters, else a cut at a word within 120
ending in `…`; a date `dd.mm` without a year is the latest such day not after the file's
modification time (also `dd.mm.yyyy`), and it is the item's `found_on` (a file edited more than a
year later reads it as a later year, so such rows become new items); a project matches by slug or
by name; an origin outside the four is `owner` with the text as the source in words; the location
column as written (empty: none), and only without that column the first `path:line` of the what
cell (never a link, an address after `@` or a host with a domain ending and a port), is the
location; a decision
outside the vocabulary and the aliases leaves the item `new` with the cell as its triage note and a
warning; an area over 60, a source over 200 or a note over 500 characters is cut at a word with a
warning. Damaged tables lose no row: after a row cut short (no closing pipe, or fewer cells than
the header less one), a line without a leading pipe continues its last cell, and after a complete
row such a line is a row of its own; reading rows after a blank line stops at the header of a
table of its own; after a
blank line, lines with the header's cells (or one fewer) and a project cell (a row typed as a list
item `- …` included) stay rows of the table above, and one the import cannot place (unknown
project, bad date) is `skipped`, other table-like lines (a legend) are left out; each with
a warning, never an error. The plan ends with one line per file, `<file>: N rows read: add
a, …`, counting every row. Import: one item per row, identity project + date + title slug (tracked
in `import_item`), the decision as the status (`planned` records history only, no task is created);
an unknown project, a bad date, a what over 20000 characters is an `error` line, a secret value
`refused`; a row changed on the board since its import is a `conflict` like other
items. An item no import wrote (made on the board) is matched by the same identity: the same text is
`unchanged` with bookkeeping, a different one a `conflict` (or an update with `--overwrite`), never a
second item. An item planned on the board (it has a task) is never moved back by an import, also with
`--overwrite` (`skipped`). Export writes the same table with every item, uncapped, oldest first, in
eleven columns: the what cell is the whole body when the title was made from it, the origin the
source in words when there is one; an import of the export changes nothing.

**One project's rows** (design 0027). `relaybay import --backlog <file> --project <slug>` (or the
project's name) reads only the rows of that project; the others are `skipped` as "other project",
an unknown project of `--project` stops the run. A row it would create is matched against the
project's items in `new`, `later` or `planned` that no other row of the file stands for: the row's
text names the item (`#N`, `backlog #N`, any word before the number but `PR`, `pull`, `request`,
`issue`, `mr`), its title is like the item's (`title_similarity` 0.6 and up, as `backlog_add`), or
the row's text holds the item's title words (stems of four letters or more, stop words left out, a
title with at least two of them) at a share of 0.7 and up. Pairs are taken best first (a name before
a score), one row per item. A matched row is `skipped` with `matches #N: names it`, `title 0.71` or
`cover 0.80`; `--apply` records it in `import_item` against that item (its content hash starts with
`match:`) and never writes the item, so the next import shows it `unchanged`. A row whose best
score is 0.4 or more without a match (or whose match another row took) is created, with a
plan warning `<file>:<line>: created as a new item, but it is near #N (0.45)` (only there, not on
the row's line). Matches and near misses count in the plan hash. Without `--project` nothing is
matched.

`--match LINE=N` (repeatable, with `--backlog` and `--project` only) decides one row by hand: the
row on line LINE of the registry is matched to item N (`matches #N: by hand`) before any automatic
match, and that item is no longer a candidate for other rows; `--match LINE=new` creates the row
even when it would match (no near warning either). A row matched by an earlier import and now
named otherwise by `--match` is matched afresh. It is an `error` line in the plan when N is not an
item, is an item of another project, is not in `new`, `later` or `planned`, is already another
row's item (by identity or by another `--match`), or the row is already an item of its own, is of
another project, or LINE is not a row of the file (`<file>:<line>: --match LINE: no row on that
line`). A malformed value or one line given twice with two values stops the run (exit 2). The
`--match` decisions count in the plan hash, so `--apply --expect` writes only the plan approved.

The command line prints UTF-8 when stdout or stderr is a pipe or a file in another encoding (a
Windows code page), with characters it cannot encode replaced, never a `UnicodeEncodeError`.

<a id="spec-desktop"></a>

## 13. Desktop program

The design is [0016](design/0016-desktop-program.md). macOS and Linux builds are beta: checked by
CI smoke tests only.

The installed program (`relaybay-desktop`) and `relaybay desktop` (needs the `relaybay[desktop]`
extra, else it prints how to install it and exits `2`) run the server of `relaybay serve` in a thread of a program with a board window (the system
web view) and a tray icon. The window signs in with the login link and keeps no cookies; closing it
hides it, **Quit relaybay** in the tray stops the server and the program. A stop of the server (Quit,
Restart server, and Ctrl+C on `relaybay serve`) ends the board's `/events` streams first, so it never
waits the graceful timeout for them nor cuts them with a traceback; the browsers reconnect. An open
MCP stream still holds the stop for the graceful timeout (2 s). The tray menu: Open board,
Open in browser (the link `relaybay token` prints), Open Inbox (signs in and goes to `/inbox`, in the
window or, without one, the browser), Quiet notifications (a checkbox, [below](#spec-desktop-notify),
absent with `notify = false`), Restart server (Reconnect for an external
server), Logs (`<data dir>/logs/relaybay.log`, 5 x 1 MB, the login token redacted), Settings (Start
at login, the data folder, the server and the update check in a local form, and Reconnect Claude
Code, [below](#spec-desktop-setup); Check for updates),
Update to X.Y.Z when a newer release is out ([below](#spec-updates)) and Quit. When the port is taken the
program starts no second server: the window names the holder (another relaybay and its version, or
another program). One program per user: a second start shows the first one's window and exits;
`relaybay desktop --quit` asks the running one to quit. `--background` starts hidden; start at login
writes the Windows Run key, a macOS LaunchAgent or an XDG autostart file, rewritten with the current
executable on each start.

<a id="spec-desktop-watchdog"></a>

**Watchdog.** A background loop of the program, ended by Quit. Embedded server: every 30 s it asks
its own `/healthz` (5 s timeout); two failures in a row, or a server thread that is no longer alive
(also after a start that failed), log `watchdog: <reason>; restarting the server` and restart the
server exactly as **Restart server** does (the same lock; a check while a restart runs, or one
during which Quit or a manual restart came, counts nothing).
At most 3 automatic restarts in 10 minutes; the next failure shows the problem page "The server
keeps failing: see the log <log file>" and the same text as a tray notification, and the watchdog
restarts nothing more until the owner's **Restart server** (or a Settings save), which also resets
the count. A taken port is a setting to change, not a crash: nothing is restarted for it. External
server: never a restart; every 60 s the same `/healthz` check sets the tray tooltip to "relaybay ·
server unreachable since <local time, YYYY-MM-DD HH:MM>" from the first failed check, and back to
"relaybay" once it answers; the first failed check also gives one tray notification (kind `server`,
[below](#spec-desktop-notify)).

<a id="spec-desktop-notify"></a>

**Tray notifications** (design [0020](design/0020-tray-notifications.md)). A background loop of the
program, ended by Quit: every 5 s it reads `GET /api/notifications` over HTTP (loopback without a
proxy, as `/healthz`), the same way for the embedded and an external server (an external server needs
this version or newer). What becomes a notice:

| Kind | Event | Title | Text |
|---|---|---|---|
| `waiting` | `task.updated` setting `waiting_on` to one of `owner_names` (matched as the Inbox does) | `Waiting on you: <key>` | the task's title |
| `review` | `task.stage_changed` to `review` or `test` | `Waiting for your acceptance: <key>` | the task's title |
| `reply` | `comment.added` by an agent | `Reply on <key> from <session>` | the comment's first 200 characters |
| `question` | `question.asked` of a blocking question (a look question always is); a non-blocking one goes on with its default and is in the Inbox only | `Question on <key> from <session>`, or `Look at this on <key> from <session>` | the question's text |
| `approval` | `approval.requested` of a new request (not when an open one is filled in) | `Approval on <key> from <session>` | what and where, e.g. `push of feat/x in backend` |
| `permission` | `session.status_changed` to `waiting` (the `Notification` hook, which an idle prompt raises too); off by default | `<session> is waiting` | the session's activity |
| `server` | the program itself: an external server stops answering (at most one until it answers again) | `The relaybay server is unreachable` | `<URL> does not answer.` |

A write by the owner himself is no notice (actor `board`, `owner` or one of `owner_names`).
`notify_kinds` in `desktop.toml` chooses the kinds; a kind removed in 0.12 (`reminder`, `brake`,
`budget`, `answer`) is dropped when the file loads, without an error. `notify = false` stops the polling and the `server`
notice and forgets the cursor, so switching it on starts from the newest event. Both are read at the
start and on Restart server. The "keeps failing" notification of the watchdog does not depend on them.

*Grouping.* At most one toast per 20 s window, the first at once. One item shows its title and text;
several show the title `relaybay` and `3 need you: 2 reviews, 1 reply`. The same task and kind count
once in a window, with the newest text; a `review`, a `question` or an `approval` absorbs a
`waiting` of the same task, since a move to review, a blocking question and a request usually set
both. Everything new is read before anything is shown, so a downtime ends in one
toast. A balloon's title stops at 63 and its text at 255 UTF-16 units, ending in `...`.

*Quiet mode.* The tray's **Quiet notifications** (`notify_quiet`): no toast, an accent-coloured dot in
the upper right corner of the icon while something is held (the update dot stays lower right), and on
leaving it one toast at once. It survives a restart, with what it held.

*The cursor.* `notify_state.json` in the config folder holds the last event id, what is waiting to be
shown and which board that is (base URL and data folder). A first start, or another board, starts from
the newest event and tells nothing of the past. A failed poll keeps the cursor, logs
`tray notifications paused: <reason>` once and `tray notifications resumed` when it works again; a
missing or unreadable file means a first start, with a warning for an unreadable one.

*Limits.* A click on a balloon does nothing: pystray offers no such event on Windows, hence Open Inbox.
pystray's X11 backend (the `.deb`) has no notifications: there a notice is only a line in the log, as it
is on every system (`notification: <heading>: <text>`). The operating system's Do not disturb may hide a
toast. A board tab with browser notifications on tells a review as well.

**`GET /api/notifications?after=<id>&limit=100`** (Bearer token, `limit` from 1 to 500):
`{"last_id": n, "items": [{"id", "kind", "title", "text", "url", "key"}]}`, oldest first. `id` is the
event's, `url` a path on the board, `key` the task key (for `permission`, the session's name). `limit`
counts the events read, of which some are no notice, so `items` can be empty while `last_id` moves on;
call again with `after=<last_id>` until it stops moving. Without `after`, no items and the newest id. An
`after` past the newest event (another database) gives the newest id, which is lower. `server` never
comes from here.

<a id="spec-desktop-start-journal"></a>

**Start journal.** Each start of the program (not `--quit`) appends to `desktop-start.log` in the
config folder, before the log file is set up, so a start that ends without a window or a log line
still leaves a trace. Lines are `<local ISO time> [<pid>] <message>`: first
`start relaybay <version> argv=[...] cwd=... frozen=... executable=...`, then one outcome —
`another instance answered`, `lock held, no answer (desktop.ipc written <time>` or
`no desktop.ipc)`, `extra missing`, `started, log <log file>` (once the log file is configured), or
`exception: <type>: <message>` followed by its traceback indented by two spaces (the exception is then
raised as before) — then `quit: <who asked>` when the program is asked to end (`tray menu`,
`another run's --quit`, the same with `, for the update installer` while an installer waits, `update`,
`the window loop ended`, `--exit-after`; the log file has the same line), and `exit <code>` when the
start returns. Above 64 KB the file keeps its newest
32 KB. A failure to write it never stops the start; once the log file exists it is a warning there.
`relaybay doctor` shows the newest start and the lines its process wrote after it as the
`desktop start` item ("no start journal yet" without the file); the item fails on an exception, or
when nothing follows the start line (the process ended before any outcome).

<a id="spec-desktop-window"></a>

**Theme.** The program's own pages (starting, problem, Settings) and the window background before a
page paints follow the machine's theme (Windows `AppsUseLightTheme`, macOS `AppleInterfaceStyle`;
elsewhere the background is light and the pages follow the web view's `prefers-color-scheme`). On
Windows from build 18362 the tray menu follows the system setting through the undocumented
`uxtheme.dll` exports 135 (`SetPreferredAppMode`, force dark) and 136 (`FlushMenuThemes`), set once
before the tray icon is created when the machine is in the dark theme, so a change of theme shows
after a restart; on an older build or any failure the menu stays light. macOS and
Linux draw their menus by the desktop's theme.

**Window state.** Show from the tray keeps the window's state: a maximized window stays maximized, a
minimized one returns to what it was. The board window (not Settings) keeps its maximized flag and
normal (not maximized) `x`, `y`, `width` and `height` across restarts in `window-state.json` beside
`desktop.toml`, written atomically when the window closes or hides and on Quit, not while it is
resized. A missing or corrupt file means the defaults (1280 x 860, centred); a size below the minimum
(640 x 480) is raised to it and a size above the largest screen is lowered to it; a position with the
middle of the title bar off every current screen is dropped, so the window is centred; no failure
reading the file or the screens stops the program. The geometry of a maximized or minimized window never overwrites
the normal one, and a move or resize within 1 s before a maximize or minimize does not count. A
window closed while minimized comes back un-minimized. `--background` still starts hidden, and the
first show of a window saved as maximized is maximized.

**The window after an update.** When the program quits for an update it started (the installer's
`desktop --quit` on Windows, the program's own quit after opening the `.dmg` or `.deb`) and the board
window is on screen (minimized counts, hidden to the tray does not), it writes the one-shot marker
`reopen-window` (the time) beside `window-state.json`. The next start removes it before it creates
the window, and a `--background` start that found it at most 10 minutes old shows the window like a
plain start, with its saved
geometry and maximized flag, so the window open before a Windows update comes back while a later
logon start stays hidden. Tray Quit, `--quit` from a terminal and an installer run by hand write
no marker: only a quit within 60 s after Update has started the installer does (the silent setup
sends its `--quit` within seconds; one that never does was cancelled). A marker that cannot be
removed is ignored (logged), so it never shows the window at every logon; an older or unreadable
marker (macOS and Linux, where no start follows the quit by itself) is removed without showing it,
so a logon start the next day stays hidden. On macOS the flag is not restored: pywebview
calls only full screen maximized there. On Linux the position is not restored: pywebview reports and
takes it in different coordinates, so only the size and the flag are kept.

<a id="spec-desktop-install"></a>

**Installers.** A release attaches `relaybay-<v>-windows-x64-setup.exe`,
`relaybay-<v>-macos-arm64.dmg` and `relaybay_<v>_amd64.deb` with `SHA256SUMS.txt`; none is signed
with a certificate. The program is `relaybay-desktop` (the bundle's executable on macOS); the
command line `relaybay` and the hook forwarder `relaybay-hook` sit next to it.

| OS | Installed to | Notes |
|---|---|---|
| Windows x64 | `%LOCALAPPDATA%\Programs\relaybay` (per user, no admin rights) | Start Menu entry; options "Start relaybay at login" (off) and "Add relaybay to PATH for this user" (on). SmartScreen warns about an unknown publisher: **More info → Run anyway**. |
| macOS arm64 (beta) | `/Applications/relaybay.app` (dragged from the `.dmg`) | Ad-hoc signed, not notarized: the first start is blocked; System Settings → Privacy & Security → **Open Anyway**, or `xattr -dr com.apple.quarantine /Applications/relaybay.app`. |
| Linux amd64 `.deb` (beta) | `/opt/relaybay`, `/usr/bin/relaybay` | `sudo apt install ./relaybay_<v>_amd64.deb`. Tray icon and the board in the browser, no window; GNOME shows the tray only with the AppIndicator extension. |

Uninstalling (Windows: Settings → Apps; macOS: delete the app; Linux: `sudo apt remove relaybay`)
keeps the data folder and `desktop.toml`; `relaybay setup --remove` takes the hooks out first.

<a id="spec-desktop-settings"></a>

**`desktop.toml`.** In the per-user config folder (or `RELAYBAY_CONFIG_DIR`);
`relaybay desktop config` shows and changes it. The data folder is `--home`, else `RELAYBAY_HOME`,
else `data_dir` in `desktop.toml`, else the per-user data dir; changing `data_dir` moves nothing.
With `server_mode = "external"` the program opens `server_url` with the token saved in
`external-token` (0600) beside the file, and the CLI's `token`, `hook`, `setup` and `doctor` use
that server too; `relaybay serve` and the program's own server ignore the mode,
`relaybay token --rotate` is refused and a missing token is an error in `token` and `setup` (exit
`2`). Plain `http://` to another machine is allowed with a warning (Settings, `desktop config`, the
log): the token travels in clear text. `data_dir` must be an absolute path. A server never fails
over `desktop.toml`: keys of a newer version are skipped, a broken file is a logged warning and its
settings are left out; the CLI names the problem, and `relaybay desktop config` with options writes
the file anew. Keys: [configuration reference](reference/configuration.md#desktop-program-desktoptoml).

<a id="spec-desktop-setup"></a>

**Setup from the installed program.** `relaybay setup` run by the installed `relaybay`, the
board's Integrations page of its embedded server and Reconnect (below) write command hooks that
start the `relaybay-hook` next to the program by its full path ([§5](#spec-setup-writes)):
`%LOCALAPPDATA%\Programs\relaybay\relaybay-hook.exe` on Windows,
`/Applications/relaybay.app/Contents/MacOS/relaybay-hook` on macOS, `/opt/relaybay/relaybay-hook`
on Linux. Entries of a source checkout or another install are replaced in place. Setup writes
nothing (an error in the plan) while the program runs from a place that goes away: a macOS bundle
under `/Volumes/` (the mounted disk image) or in `AppTranslocation` ("move relaybay.app to
/Applications first"), or a program under the temp folder or `Downloads` (install it with the
setup program or the `.deb`); `--remove` always works. In external mode the hooks point at
`server_url` and the `external-token` file, and the MCP registration stays the printed
`claude mcp add` command with the external URL.

**Start-up check and Reconnect.** At each start the program reads relaybay's entries in Claude
Code's files (the Claude Code items of `doctor`; a failing item is a log line). When the command
hooks start a forwarder that is missing, fails or answers another version, or, in the installed
program, is not its own `relaybay-hook`, the tray shows "Claude Code hooks point to `<path>`: open
Settings -> Reconnect". The program never writes them on its own. Settings → **Reconnect Claude
Code** shows the plan and diff of `relaybay setup` for this program (its forwarder, its server URL
and token file, the hook type already installed or command) and writes on **Apply** exactly the
plan shown (its hash), with the backup of [§5](#spec-setup-writes); a file changed meanwhile is
refused.

<a id="spec-updates"></a>

**Update check.** Every server (`serve`, the program's embedded server, Docker) asks
`https://api.github.com/repos/Codenator81/relaybay/releases/latest` 30 s after the start and then
every `update_check_hours` (default 24, from 1 to 720), without a token, with `User-Agent: relaybay/<version>`, a
5 s timeout and the system proxy; `update_check = false` in `config.toml` (or
`RELAYBAY_UPDATE_CHECK=false`) turns it off. The answer is kept in memory only. A failure (offline,
the API's rate limit, a malformed answer) is a debug log line and keeps the last answer. Versions
compare in PEP 440 order (`0.6.0rc1` < `0.6.0`); GitHub's latest release leaves pre-releases out.
A newer release shows one line at the top of the Inbox, not counted in its badge or in the
digests: "relaybay X.Y.Z available", linked to the release page with its notes, and the upgrade
command of this install: `uv tool upgrade relaybay`, `pipx upgrade relaybay`,
`pip install -U relaybay`, `uvx relaybay@latest`,
`docker pull ghcr.io/codenator81/relaybay:X.Y.Z`, `git pull && uv sync` in a checkout; the
installed program says "use Update to X.Y.Z in the relaybay tray menu, or Settings → Check for
updates".
`GET /update` (board cookie or token) answers the same state as JSON: `current`, `latest`,
`available`, `url`, `checked_at`, `error`, `enabled`, `command`.

The program checks its own version the same way (`update_check` and `update_check_hours` in
`desktop.toml`; at their defaults these keys are not written, so an older program still reads
the file), so in external mode the tray speaks for the program and the Inbox for the server.
A newer release puts a dot on the tray icon and **Update to X.Y.Z** in the tray menu; Settings →
**Check for updates** checks now and answers in a notification. Update (a click, never on its own)
in the installed program downloads this system's installer (`relaybay-X.Y.Z-windows-x64-setup.exe`,
`relaybay-X.Y.Z-macos-arm64.dmg`, `relaybay_X.Y.Z_amd64.deb`; at most 200 MB, HTTPS, only from the
project's release download URLs) into the per-user cache folder (`updates/`, emptied of earlier
downloads first and again at the next start of the program), checks its SHA-256
against `SHA256SUMS.txt` of the same release and deletes it on a mismatch or when it is not
listed: that file never runs, and the tray says why. A verified installer starts detached: Windows
runs `setup.exe /SILENT /CURRENTUSER`, which quits the running program itself and starts the new
one afterwards; macOS opens the `.dmg` and Linux the `.deb` with the OS's opener, and the program
quits. The SHA-256 from the same release is integrity only: nothing is signed. A program that is
not the installed one (run from a uv tool, pipx or a checkout) shows its upgrade command instead.
A second click while a download or an installer runs is ignored, and Windows setup runs one at a
time (`SetupMutex`).

**Progress and Cancel.** Update says it started in a notification with the installer's size
("Downloading relaybay X.Y.Z (39 MB)..."). While it downloads, the tray tooltip shows
`Downloading X.Y.Z: 12 of 39 MB (30 %)` (only the megabytes when the size is not known; redrawn at
most twice a second), and the tray menu shows a greyed line in place of Update,
`Downloading X.Y.Z (39 MB): 30 %` in 10 % steps (the menu is rebuilt only for a new step, so one
held open stays usable), then `Checking the download of X.Y.Z...` during the SHA-256 check, and
**Cancel update**. Cancel update, or Quit, stops the download at its next read (64 KiB; opening the
connection or reading `SHA256SUMS.txt` may take up to its 60 s timeout first), also during the
check: the partial or verified file is deleted, nothing runs, and a notification says that nothing
was installed (none after Quit). A failed download or check says what failed and that nothing was
changed; a tray notification longer than Windows takes (255 characters) is cut, the log has it
whole. Once the installer has started there is nothing to cancel; the installer and
the backup before a new version work as before.

<a id="spec-backups"></a>

**Backup before a new version.** `<data>/last_version` holds the version that last started. When a
database exists and `last_version` is missing or names another version, the server copies the
database (SQLite's backup API, consistent while a WAL is live), `config.toml`, `policy.json` and
`token` into `<data>/backups/<old>-to-<new>-<YYYYMMDD-HHMMSS>/` (local time, the clock of the log;
`unknown` when there was no
`last_version` or it is not a version; `-2`, `-3` appended when the name is taken) before it
migrates; `last_version` is written only after the migration succeeded. A step that already has
its backup (the same `<old>-to-<new>`, left by a start that failed) is not copied again, so
repeated failed starts never rotate the real pre-upgrade copy away. Only the newest `backup_keep`
(default 5) of these folders are kept, deleted one by one: a folder that cannot be deleted (a file
held open) is a logged warning and never stops the start. Only names of that exact pattern with
real version numbers count; anything else under `backups/` or in the data folder (hand-made
backups) is never touched or counted. `backup_keep = 0` takes none. A failed copy stops the start
and leaves no partial folder. Attachments are not copied. This covers `serve`, the program, Docker
and `relaybay import --apply` (which migrates the real database too); to go back, stop the server
and copy the files from a backup folder into the data folder.

# relaybay specification

How relaybay behaves **now**: the concepts and every shipped feature. This page never describes
plans; upcoming work lives in the [roadmap](ROADMAP.md) and the [design documents](design/index.md).
When a feature ships, its final behaviour moves here in the same pull request.

Version covered: `0.1.0`. Anchors of the form `#spec-…` are stable and may be
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
progress, priority, the external tracker fields and a `version` that grows with every write. The
editable fields come from one list (`domain/task_fields.py`) that drives write validation, the
`relaybay://schema/task` resource and the "Editable fields" line of `task_get`.

<a id="spec-stages"></a>

**Stage.** A fixed set: `plan → dev → test → live → done`. Any transition is allowed (work jumps back
from `test` to `dev`); the event log keeps the history. Moving to `done` sets the closed time.
Display labels are configurable ([§10](#spec-config)); the keys stay English.

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

**Decision.** A short text recorded for a project (optionally a task) with `decided_by`; shown in
the project snapshot and in `task_get`.

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
`live.action`. A stage change that misses a rule carries the warnings in its payload.

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
**Sessions** and **Stands & access**. The current item is highlighted (`aria-current="page"`). The header button hides or
shows the sidebar on wide screens; the choice is remembered in the browser (`localStorage`, the
sidebar is shown when storage is unavailable). Below 760 px the sidebar is a drawer opened by the
same button and closed by Esc, a click outside or following a link. Items whose feature does not
exist yet are not shown.

<a id="spec-board-pages"></a>

| Page | URL | Content |
|---|---|---|
| Projects | `/` | Projects with task counts per stage. |
| Project board | `/p/<slug>` | One column per stage; on narrow screens a stage tab bar shows one column at a time. |
| Task dialog | `/p/<slug>?task=<key>` | The task over the live board (full screen on a phone); closes with Back, Esc or a click outside. |
| Task page | `/t/<key>` | The same details as the dialog on a page of its own. |
| Inbox | `/inbox` | Everything that needs the owner, across projects ([below](#spec-board-inbox)). |
| Sessions | `/p/<slug>/sessions`, `/sessions` | The sessions of a project, or all sessions ([below](#spec-board-sessions)). |
| Activity | `/activity` | The event feed and the actions on live ([below](#spec-board-activity)). |
| Stands & access | `/p/<slug>/environments` | Environments, access map, permission levels and agent proposals ([§7](#spec-environments-page)). |
| Integrations | `/settings/integrations` | Claude Code on this machine ([§5](#spec-setup)); `/settings` redirects here. |
| Sign-in | `/login` | Token form; also accepts `?token=…` once and sets the cookie. |

<a id="spec-board-cards"></a>

**Cards** show only the essentials: key, tracker badge (`<tracker_name> <tracker_ref>`, a link when
a URL is set, ✓ when logged, "not logged" with `require_tracker_log`) and an icon-only "open in
desktop" link with the session's hook status dot in the top row; then title, waiting on, branch and
the next step (two lines, muted with "stale" when stale). Done cards hide waiting on and the next
step.

<a id="spec-board-dialog"></a>

**Task dialog / page** shows every field (stage with note, waiting on, next step, branch, tracker,
document path, updated time and version), description and progress in full, the latest reports
with state and size badges, decisions, the bound session with its hook status and last activity,
"Open in desktop" and "Copy link", the session binding history, the tracker history, an
**Unbind session** button (with confirmation), the comments and the edit form. A stage move that
misses a rule shows the warning in the form (or the refusal in `strict` mode).

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

Archived done tasks are left out. Task rows open the task dialog (`/p/<slug>?task=<key>`). The
sidebar badge shows the total number of rows (hidden at zero); badge and list refetch on events that
can change them (`task.*`, `session.seen`, `session.status_changed`, `comment.added`,
`comment.read`, `environment.proposed`, `environment.proposal_*`), at most once per 2 s. With
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

**Server instructions** (sent on connect) are the working contract, nine numbered points: read
`project_snapshot` first (its first line names the rules); pick or create a task and pass `session`
(desktop id with `session_title`, else a name; `hook_session` if the session start gave one; don't
rebind without your `local_` id); update `next_step`, `waiting_on` and `progress` on every stage
change and report; report at the end of every stage with the configured first line, states and
sizes (taken from the effective global rules); set `waiting_on` to who decides when a decision is
needed; use `task_patch` for fields a cached schema lacks; fix what a `Warning (rules)` line names;
when a hook gives owner comments, handle them like a user message and call `comment_ack` (or answer
with `comment_add`); before touching a server, cluster or database read `environments_get` (the
level says what is allowed unasked; live is read-only unless the owner set otherwise), never paste
secrets, propose changes with `environment_propose`.

<a id="spec-mcp-tools"></a>

**Tools.**

| Group | Tools |
|---|---|
| Projects | `project_list`, `project_upsert`, `project_snapshot` |
| Tasks | `task_list`, `task_get`, `task_create`, `task_update`, `task_patch`, `task_set_stage` |
| Sessions | `task_bind_session`, `task_unbind_session` |
| Reports and decisions | `report_add`, `decision_add` |
| Comments | `comments_list`, `comment_ack`, `comment_add` |
| Environments | `environments_get`, `environment_propose` |
| Catch-up and rules | `events_since`, `rules_get` |

Read tools are annotated read-only, write tools non-destructive; none reaches outside relaybay.
Write tools take optional `session`, `session_title` and `hook_session`. `"-"` (or `""`) clears an
optional text field, because models cannot always send an empty string. `task_update`,
`task_patch` accept `expected_version` and fail instead of overwriting a newer version; otherwise the
last write wins.

<a id="spec-mcp-resources"></a>

**Resources.** `relaybay://project/{slug}` (same markdown as `project_snapshot`),
`relaybay://project/{slug}/environments` (same as `environments_get`),
`relaybay://rules` and `relaybay://rules/{project}` (same as `rules_get`), `relaybay://schema/task`
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
| `SessionEnd` | ended | the session is unbound from every task it is bound to (history kept, `task.session_unbound` with `reason: "session_end"`), for every session kind and end reason |

Other events are ignored. Activity of a working session is written at most once per 5 s per session
(the latest wins); status changes are written at once, as `session.status_changed` per bound task.

<a id="spec-hooks-linking"></a>

**Linking a hook session to a relaybay session**, in this order:

1. Exact: a write passing `hook_session` (with `session`) links that Claude Code session id to the
   calling session; a row the hooks had created for it by guess hands over its status and is
   removed. An id that another live (not ended) session already linked exactly is not moved: the
   write succeeds and its response gets `Warning: hook_session <id> belongs to another session
   (<name>); not relinked.` `task_bind_session` never links a hook id (the caller may be binding
   another session); it answers with a note when one is passed.
2. Fallback on first sight: the one desktop session bound to a task in the project whose
   `repo_path` contains the hook's `cwd`.
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

<a id="spec-hooks-wake"></a>

**Wake-up hook (opt-in, experimental; not verified against a live Claude Code).** With
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
version that supports it, so `--wake` is refused unless `--force-wake` is given too
(`MIN_ASYNC_REWAKE_VERSION` is unset); `setup --check` and `doctor` show the installed hook as
`UNVERIFIED`. If turns end only after a long wait, run `relaybay setup --apply --no-wake`.

<a id="spec-setup"></a>

## 5. Setup and integrations

`relaybay setup` connects Claude Code on this machine. It writes only the user settings file
`~/.claude/settings.json` (read by the CLI and the desktop app's local sessions), and only on an
explicit command or button.

<a id="spec-setup-cli"></a>

| Command | Effect |
|---|---|
| `relaybay setup` (`--dry-run`) | Detection (`claude` on `PATH` and its version, the desktop app, whether http hooks are supported), the exact diff of the settings file and its plan hash. Writes nothing. |
| `relaybay setup --apply [--expect <hash>] [--mode command\|http] [--wake [--force-wake] \| --no-wake]` | Backs the file up, then adds relaybay's hook entries. `--wake` also adds the wake-up hook ([§4](#spec-hooks-wake); command mode only; refused without `--force-wake` while no Claude Code version is known to support `asyncRewake`), `--no-wake` removes it; without either an existing wake-up hook is kept. |
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
wake-up hook (an ok line starting with `UNVERIFIED` and the fallback risk, only when installed;
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
`acme-shop` (with a repository path) and `demo-api`, eight tasks across all five stages with
descriptions, progress and reports, tracker badges (one logged done task), a stale next step, a
decision and a desktop session bound to a dev task and shown as working (one synthetic hook event).
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
| `stage_requirements` | per target stage, a list of `{kind = "report_since_stage_change"}` or `{kind = "field_present", field = "<task field>"}` | `test`: a report since the last stage change; `done`: `progress` |
| `description_template` | markdown template for new task descriptions | none |
| `guidance` | free-text markdown for agents | one short paragraph |

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
`Rules <hash>: report before test, progress before done (read relaybay://rules/<project>)`.

<a id="spec-rules-modes"></a>

**Checks and modes:** requirements are checked on stage moves only (`task_set_stage`, `task_update`
/ `task_patch` with a stage, the board form). `rules_mode = "warn"` (default): the move succeeds and
the response, the board form and the `task.stage_changed` event carry the warnings.
`rules_mode = "strict"`: the move is refused with the reason and nothing changes. Report format is
not checked on `report_add` in this release.

<a id="spec-environments"></a>

## 7. Environments and access

Design: [Environments and access](design/0006-environments-and-access.md).

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
access entry or a proposal is checked: a field whose name contains `pass`, `pwd`, `token`,
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
(`_SECRET_SPOTS`). A value that is a reference as a whole always passes. The refusal names the
field and never repeats the value. These are pattern checks: a secret in an unusual shape (e.g. in
plain prose) can pass.

<a id="spec-permissions"></a>

**Permission levels.** Per environment, `none` (do not touch), `read` (look only), `ask` (the
owner approves each action) or `full` (act as needed); optionally per capability `sign_in`, `ssh`,
`kubectl`, `db`, `deploy`. An environment without its own level uses `permission_defaults` from the
configuration (default: local and test `full`, live `read`); a capability without an override uses
the environment's level. Only a person changes levels, on the board, and some writes need a
second confirmation: on an environment that becomes or stays live, an explicit level or override
above `read` that is higher than before (a level that comes from `permission_defaults` does not
ask); on a live environment whose kind changes, any capability whose effective level, defaults
included, rises above `read` (live → test with the defaults turns `read` into `full`). Accepting
a proposal asks the same. Agents propose changes instead (below).

<a id="spec-environments-page"></a>

**Stands & access page** (`/p/<slug>/environments`, sidebar item under each project): one panel per
environment with its kind, effective level ("(default)" when it comes from the configuration),
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

**Guard (PreToolUse).** `guard_mode` (`off` / `warn` / `block`, default `warn`). The existing
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

Mutating heuristics (one table, `domain/guard.py`; they see text only). A rule applies when its
change follows its command in the same command segment (the text split at `|`, `;`, `&` and
newlines outside quotes), so `ssh app uptime; rm -rf build` is not a remote change:

| Rule | Capability | Pattern (simplified) |
|---|---|---|
| kubectl change | `kubectl` | `kubectl … apply / create / delete / edit / patch / replace / scale / set / label / annotate / drain / cordon / uncordon / taint / exec / cp / rollout restart / undo / pause / resume` |
| helm change | `deploy` | `helm … upgrade / install / uninstall / delete / rollback` |
| remote change over ssh | `ssh` | `ssh …` with `rm`, `mv`, `cp`, `dd`, `tee`, `truncate`, `kill`, `reboot`, `shutdown`, `chmod`, `chown`, package managers, `sudo`, `crontab`, `systemctl start/stop/restart/reload/enable/disable/kill/mask`, `service … start/stop/restart/reload`, `docker rm/stop/restart/run/kill/exec/compose/pull`, `sed -i` |
| file copy to a host | `ssh` | `scp` / `rsync` whose last argument is remote (`host:path`); copying from a host is a read |
| file transfer session | `ssh` | `sftp` |
| SQL change | `db` | after a database client (`psql`, `mysql`, `mariadb`, `sqlite3`, `sqlcmd`, `mongosh`, `redis-cli`, `clickhouse-client`, `cqlsh`, …; in MCP tool input also without one): `insert into`, `update … set`, `delete from`, `drop`, `alter`, `truncate`, `create table/database/index/user/role`, `grant`, `revoke`, `flushall`, `flushdb` |
| git push | `deploy` | `git … push` |
| terraform change | `deploy` | `terraform apply / destroy / import` |
| MCP tool by name | — | an MCP tool whose name has a word such as `create`, `update`, `delete`, `apply`, `deploy`, `exec`, `run`, `restart`, `scale`, `push` |

Decisions per touched environment: `full` → nothing; `read` → nothing unless a rule matched;
`ask` → ask; `none`, or `read` with a rule → deny in `block` mode, a warning in `warn` mode. The
strongest decision wins (deny > ask > warn). The answer is
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
recorded as a `live.action` event (any decision, including the calls that go on): project,
environment, the bound task of that project (the first, if several; none when the session has no
task there), tool name, the command — whitespace collapsed, obvious secrets masked (private key
blocks, known token shapes, `token <value>` and every place the secret check above refuses, quoted
values whole: `PGPASSWORD='***'`, `mysql -p'***'`, `sshpass -p ***`, `curl -u admin:***`, …), cut
at 200 characters — the decision (`allow`,
`warn`, `ask`, `deny`), the level and the matched rule. Activity shows the newest 20 as **Actions on
live** above the feed (with the project filter, refreshed live, linking to `?kind=live.action` for
all), and the task dialog and page show the task's own as **Actions on live**.

<a id="spec-environments-mcp"></a>

**MCP.** `environments_get(project)` and the resource `relaybay://project/{slug}/environments`
return the full map (guard mode, each environment with base URL, level and overrides, sign-in flag
and login-state path, access entries, notes, last change). `project_snapshot` adds one line per
environment after the description: `- live — live · agents: read (default) · https://…`. There is
no write tool besides `environment_propose`.

<a id="spec-memory"></a>

## 8. Memory and context

Not part of this release beyond decisions ([§1](#spec-decision)) and `events_since` for catch-up.
Design: [Memory and context](design/0008-memory-and-context.md).

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
   htmx runs with eval and script tags disabled.
8. **Untrusted content:** agent markdown is rendered with raw HTML disabled; hook prompt text and tool
   input are never stored, except the redacted 200-character summary of a call that touches a live
   environment ([§7](#spec-journal)); hook bodies are capped at 256 KB.
9. **No secrets at rest:** environment, access and proposal writes refuse obvious secret values
   ([§7](#spec-access-map)).

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
`RELAYBAY_HOME` / `--home`) holds `relaybay.sqlite`, `token` and the optional `config.toml`. An
invalid value stops the start with a message naming the key. Keys, variables and flags:
[configuration reference](reference/configuration.md).

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
existing databases upgrade in place.

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

The archive is computed, not stored. Events are kept forever. Table details:
[architecture](architecture.md#data-model).

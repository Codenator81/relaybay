# relaybay specification

How relaybay behaves **now**: the concepts and every shipped feature. This page never describes
plans; upcoming work lives in the [roadmap](ROADMAP.md) and the [design documents](design/index.md).
When a feature ships, its final behaviour moves here in the same pull request.

Version covered: `0.1.0.dev0` (unreleased `main`). Anchors of the form `#spec-…` are stable and may be
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
`session.status_changed`, `session.alias_added`. A stage change that misses a rule carries the
warnings in its payload.

Not shipped yet, therefore not concepts of this release: environments and access levels, comments
to agents, the review checklist, memory notes and documents (see the [roadmap](ROADMAP.md)).

<a id="spec-board"></a>

## 2. Board

Server-rendered HTML (Jinja2 + htmx, [ADR-0001](adr/0001-htmx-sse.md)), light and dark theme from
the system setting, usable on a phone. A header bar holds the brand link, breadcrumbs, the
**Integrations** link and a live-status indicator (`connecting` / `live` / `offline`).

<a id="spec-board-pages"></a>

| Page | URL | Content |
|---|---|---|
| Projects | `/` | Projects with task counts per stage. |
| Project board | `/p/<slug>` | One column per stage; on narrow screens a stage tab bar shows one column at a time. |
| Task dialog | `/p/<slug>?task=<key>` | The task over the live board (full screen on a phone); closes with Back, Esc or a click outside. |
| Task page | `/t/<key>` | The same details as the dialog on a page of its own. |
| Integrations | `/settings/integrations` | Claude Code on this machine ([§5](#spec-setup)). |
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
**Unbind session** button (with confirmation) and the edit form. A stage move that misses a rule
shows the warning in the form (or the refusal in `strict` mode).

<a id="spec-board-done"></a>

**Done column and archive.** The done column shows tasks closed within `done_window_days` (default
7), capped at 200 with a note. Older done tasks are archived on the board only: "Show archived"
(`?archived=1`) and the search box (`?q=`, key or title, also searches the archive) bring them back.
With `require_tracker_log = true` a done task without the tracker mark is never archived.

<a id="spec-board-live"></a>

**Live updates.** The page subscribes to `GET /events?project=<slug>` (`text/event-stream`). Each
message has `id: <event id>`, `event: <kind>` and a small JSON `data` (kind, project, key, stage,
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

**Server instructions** (sent on connect) are the working contract, seven numbered points: read
`project_snapshot` first (its first line names the rules); pick or create a task and pass `session`
(desktop id with `session_title`, else a name; `hook_session` if the session start gave one; don't
rebind without your `local_` id); update `next_step`, `waiting_on` and `progress` on every stage
change and report; report at the end of every stage with the configured first line, states and
sizes (taken from the effective global rules); set `waiting_on` to who decides when a decision is
needed; use `task_patch` for fields a cached schema lacks; fix what a `Warning (rules)` line names.

<a id="spec-mcp-tools"></a>

**Tools.**

| Group | Tools |
|---|---|
| Projects | `project_list`, `project_upsert`, `project_snapshot` |
| Tasks | `task_list`, `task_get`, `task_create`, `task_update`, `task_patch`, `task_set_stage` |
| Sessions | `task_bind_session`, `task_unbind_session` |
| Reports and decisions | `report_add`, `decision_add` |
| Catch-up and rules | `events_since`, `rules_get` |

Read tools are annotated read-only, write tools non-destructive; none reaches outside relaybay.
Write tools take optional `session`, `session_title` and `hook_session`. `"-"` (or `""`) clears an
optional text field, because models cannot always send an empty string. `task_update`,
`task_patch` accept `expected_version` and fail instead of overwriting a newer version; otherwise the
last write wins.

<a id="spec-mcp-resources"></a>

**Resources.** `relaybay://project/{slug}` (same markdown as `project_snapshot`),
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
one minor version first. MCP protocol: what the pinned SDK (`mcp>=2,<3`) negotiates; the minimum
Claude Code version for http hooks is taken as 2.1.63 (unverified, see [§4](#spec-hooks)).

<a id="spec-hooks"></a>

## 4. Hooks

Claude Code hooks report session activity without the agent's help. Endpoint
`POST /hooks/claude-code`, same Bearer token and Host/Origin rules as `/mcp`; the body is the hook's
input JSON. Bodies over 256 KB are dropped with `204`; a body that is not JSON gets `400`; every other
request answers `204` (or `200` with JSON for `SessionStart`) without waiting on anything else.

<a id="spec-hooks-events"></a>

| Hook event | Session status | Activity |
|---|---|---|
| `SessionStart` | working | `started (<source>)`; the answer adds "Your relaybay hook session: <id>. Pass it as `hook_session` in relaybay writes." to the agent's context (`hookSpecificOutput.additionalContext`) |
| `UserPromptSubmit` | working | `prompt` (the prompt text is never stored) |
| `PreToolUse` | working | the tool name (the tool input is never stored) |
| `Notification` | waiting | the notification message |
| `Stop` | idle | `turn finished` |
| `SessionEnd` | ended | the session is unbound from every task it is bound to (history kept, `task.session_unbound` with `reason: "session_end"`), for every session kind and end reason |

Other events are ignored. Activity of a working session is written at most once per 5 s per session
(the latest wins); status changes are written at once, as `session.status_changed` per bound task.

<a id="spec-hooks-linking"></a>

**Linking a hook session to a relaybay session**, in this order:

1. Exact: a write passing `hook_session` (with `session`) links that Claude Code session id to the
   calling session; a row the hooks had created for it by guess hands over its status and is
   removed.
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
exits 0. http hook entries use a 2 s timeout; failures do not block the session, but Claude Code
shows a non-blocking "hook error" notice. `relaybay hook` is the same forwarder behind the CLI.

<a id="spec-setup"></a>

## 5. Setup and integrations

`relaybay setup` connects Claude Code on this machine. It writes only the user settings file
`~/.claude/settings.json` (read by the CLI and the desktop app's local sessions), and only on an
explicit command or button.

<a id="spec-setup-cli"></a>

| Command | Effect |
|---|---|
| `relaybay setup` (`--dry-run`) | Detection (`claude` on `PATH` and its version, the desktop app, whether http hooks are supported), the exact diff of the settings file and its plan hash. Writes nothing. |
| `relaybay setup --apply [--expect <hash>] [--mode command\|http]` | Backs the file up, then adds relaybay's hook entries. |
| `relaybay setup --remove [--dry-run] [--expect <hash>]` | Backs the file up, then removes only relaybay's entries. |
| `relaybay setup --check [--json]` | End-to-end check, one ok/FAIL line per item; `--json` prints a list of `{ok, name, detail}` and the exit code is non-zero when an item fails. |

Options: `--home`, `--port`, `--claude-home <folder>` (a folder holding `.claude/settings.json` and
`.claude.json`, for tests or another profile).

<a id="spec-setup-writes"></a>

**What apply writes.** One hook entry for each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`Notification`, `Stop`, `SessionEnd`, appended after existing hooks.

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
  configured host. No extra property is written; similar user hooks are never claimed.
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
`relaybay.hook_client`; in http mode, `RELAYBAY_TOKEN` set to the token in this environment; the MCP
server registered for Claude Code.

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

Not part of this release. Design: [Environments and access](design/0006-environments-and-access.md).

<a id="spec-memory"></a>

## 8. Memory and context

Not part of this release beyond decisions ([§1](#spec-decision)) and `events_since` for catch-up.
Design: [Memory and context](design/0008-memory-and-context.md).

<a id="spec-security"></a>

## 9. Security model

relaybay is a single-user local server. The security layers, in request order:

<a id="spec-security-bind"></a>

1. **Bind address.** `127.0.0.1` by default. Any other address (`--host`, LAN or phone access)
   prints a warning; the names used must be added to `allowed_hosts`. No built-in TLS.
2. **Host allow-list** (DNS rebinding): every request's `Host` must be `127.0.0.1`, `localhost`,
   `[::1]`, the bound host or a configured `allowed_hosts` name, otherwise `403`.
3. **Origin check:** a foreign `Origin` is refused (`403`) on every non-GET request and on every
   request to `/mcp`, `/api` and `/hooks`. `/mcp` also has the MCP SDK's own DNS-rebinding
   protection with the same lists.
4. **Token.** 32 random bytes (URL-safe), created on first run in `<data dir>/token`, written
   atomically with mode `0600` (on Windows the user profile ACL protects it; a POSIX file readable by
   others logs a warning). Required as `Authorization: Bearer` on `/mcp`, `/hooks` and the reserved
   `/api` prefix. `relaybay token --rotate` replaces it (restart the server).
   `/healthz` is public and returns `status`, `version` and `token_id`, a 12-hex-digit HMAC
   fingerprint that reveals nothing about the token.
5. **Browser session.** `/login?token=…` (or the sign-in form) sets an `HttpOnly`, `SameSite=Strict`
   cookie (`Secure` over https, one year) derived from the token and redirects, so the token leaves
   the address bar; the access log redacts the `/login` query. Unauthenticated page visits redirect
   to `/login?next=…` (only local paths are accepted as `next`). A Bearer header also works for the
   board's URLs.
6. **CSRF.** Cookie-authenticated non-GET requests must be same-origin (`Origin`, or
   `Sec-Fetch-Site: same-origin`); board forms carry a CSRF token derived from the token; the sign-in
   form uses a double-submit nonce.
7. **Response headers:** `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
   object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
   `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.
   htmx runs with eval and script tags disabled.
8. **Untrusted content:** agent markdown is rendered with raw HTML disabled; hook prompt text and tool
   input are never stored; hook bodies are capped at 256 KB.

<a id="spec-no-telemetry"></a>

**No telemetry.** relaybay sends nothing anywhere: no analytics, no update checks, no outbound calls
at runtime, no CDN. The only network clients in the code talk to relaybay's own local server
(`relaybay token`, `relaybay setup --check`, the command hook), and they bypass system proxies.

<a id="spec-threat-model"></a>

**Threat model.** Protected against: other websites in the user's browser (Origin, CSRF, SameSite,
Host allow-list), other users on the machine (token file permissions), other devices on the network
(loopback bind by default), agent output used as an attack on the board (no raw HTML, CSP).
**Non-goals:** relaybay is not a sandbox and does not restrict what agents do on the machine; anyone
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

The archive is computed, not stored. Events are kept forever. Table details:
[architecture](architecture.md#data-model).

# relaybay — architecture plan for v1

Status: **accepted**; stage 1 in progress (see §11) · Date: 2026-11-02 · Scope: v1 (board + MCP server), outline of v2 (runner)

## 1. Goal and non-goals

relaybay gives a person who runs many Claude Code sessions across many repositories one live view of
the work: **projects → tasks → stages → reports**, and which session is doing what right now.

v1 delivers:

1. **Core** — Python 3.12+, FastAPI (async), SQLite. One command to start (`uvx relaybay`,
   `pipx run relaybay`, `python -m relaybay`) and a Docker image.
2. **Board** — browser UI: projects, tasks, stages (`plan → dev → test → live → done`); per task the
   session/agent, branch, "waiting on", last report and an event feed. Live updates with automatic
   reconnect and catch-up after sleep or a dropped connection. Usable on a phone.
3. **MCP server** — tools for a coordinating session and for project sessions: create/update a task,
   change stage, add a report or decision, get a project snapshot. Task registries and per-task notes
   that today live in markdown become data on the board.
4. **Import** — existing markdown task registries become the initial data.

Non-goals for v1:

- Starting or controlling agents (that is v2).
- Any LLM client, Agent SDK or credential handling inside relaybay. Agents are always the ordinary
  `claude` CLI under the user's own login; relaybay only listens (MCP, hooks) and, in v2, starts that
  CLI as a child process.
- Multi-user accounts, cloud hosting, issue-tracker sync.

## 2. Architecture overview

```
 Claude Code sessions (CLI, desktop app)          Browser (desktop / phone)
   │  MCP over Streamable HTTP  │  hooks (HTTP)       │ HTML (HTMX)   ▲ SSE /events
   ▼                            ▼                     ▼               │
 ┌──────────────────────── relaybay process (uvicorn) ───────────────┴──────┐
 │  /mcp   mcp_server  ─┐                                                    │
 │  /hooks hooks       ─┼─► service layer ──► SQLite (WAL)                   │
 │  /api   REST (JSON) ─┤        │  every write appends an event row         │
 │  /      web (HTMX)  ─┘        ▼                                           │
 │                         event bus (asyncio, in-process) ──► SSE fan-out   │
 └──────────────────────────────────────────────────────────────────────────┘
```

Key decisions:

- **One process, one writer.** MCP, hooks, REST and the board all live in one uvicorn process. All
  writes go through the service layer, which updates state tables and appends an `event` row in the
  same transaction, then publishes the event on an in-process bus. No second process ever writes the
  database, so there is no cross-process notification problem and SQLite locking stays trivial.
- **Events are the sync protocol.** The `event.id` (monotonic integer) is the SSE event id. A client
  that reconnects sends `Last-Event-ID`; the server replays newer events from the table. This is the
  whole catch-up story.
- **State tables + append-only event log** ("event-sourcing lite"): state tables are the source for
  reads; the log is for the feed, catch-up and audit. No projections to rebuild.

### Modules (`src/relaybay/`)

| Module | Responsibility |
|---|---|
| `__main__.py`, `cli.py` | `relaybay [serve]`, `relaybay import`, `relaybay token`, `relaybay hook`, `relaybay mcp-stdio`; argparse/typer-free stdlib CLI |
| `config.py` | Settings from defaults → `config.toml` → `RELAYBAY_*` env → CLI flags; data/config dirs via `platformdirs` |
| `db/` | Connection (aiosqlite, WAL, `foreign_keys=ON`), numbered SQL migrations, repositories |
| `domain/` | Dataclasses / pydantic models, stage enum and allowed transitions, validation |
| `service.py` | Use cases (`create_task`, `set_stage`, `add_report`, …); transaction + event append + publish |
| `events.py` | Event bus: per-subscriber `asyncio.Queue`, bounded; slow subscriber is dropped and resyncs |
| `web/` | Routes, Jinja2 templates, HTMX fragments, SSE endpoint, vendored static assets |
| `api/` | Small JSON REST API (same service layer) for scripts and the hook endpoint |
| `mcp_server/` | MCP server (`mcp` 2.x `MCPServer`), tool definitions, mounted at `/mcp` |
| `importers/` | Markdown registry importer, per-task markdown reader |
| `security.py` | Token, cookie session, Host/Origin checks, CSRF |

## 3. Data model (SQLite)

```
project   id PK, slug UNIQUE, name, repo_path NULL, description NULL,
          created_at, updated_at, archived_at NULL
task      id PK, project_id FK, key UNIQUE (e.g. "acme-shop-12"), title,
          stage TEXT CHECK(stage IN ('plan','dev','test','live','done')),
          stage_note NULL,          -- free text next to the stage ("PR open, waiting for CI")
          waiting_on NULL,          -- who the task waits for: "owner", "reviewer", a name, a session
          next_step NULL, branch NULL, doc_path NULL,   -- link to an external TASK.md
          description NULL,         -- markdown: what to do and why (1–5 sentences)
          progress NULL,            -- markdown: running summary of what is done (rewritten, not appended)
          session_id FK NULL,       -- session currently bound to the task
          priority INT DEFAULT 0, version INT,          -- optimistic concurrency
          tracker_ref NULL, tracker_name NULL,          -- issue key / tracker in an external tracker
          tracker_url NULL,         -- http(s) link to the issue, validated on write
          tracker_logged INT DEFAULT 0,                 -- the work log is entered in the tracker
          next_step_at NULL,        -- when next_step was last written
          stage_changed_at NULL,    -- when the stage last changed
          created_at, updated_at, closed_at NULL
report    id PK, task_id FK, session_id FK NULL, author TEXT,
          state TEXT NULL,          -- inspected / changed / verified / committed / pushed / blocked
          size TEXT NULL,           -- S / M / L
          summary TEXT,             -- first line, shown in snapshots and the task dialog
          body_md TEXT, created_at
decision  id PK, task_id FK NULL, project_id FK, text, decided_by, created_at
session   id PK, external_id UNIQUE NULL,   -- desktop session id (local_<uuid>) or hook session_id
          name,                     -- unique only among sessions without external_id
          project_id FK NULL, cwd NULL, kind TEXT ('desktop','cli','headless'),
          desktop_link NULL,        -- built from external_id via config desktop_link_template
          status TEXT NULL ('working','idle','waiting','ended'), activity NULL,
          last_seen_at, created_at
task_session id PK, task_id FK, session_id FK, bound_at, unbound_at NULL, bound_by
          -- binding history; at most one open row per task
session_alias name PK, session_id FK, created_at
          -- plain names that resolve to a (desktop) session
event     id INTEGER PK AUTOINCREMENT, ts, kind TEXT, project_id NULL, task_id NULL,
          session_id NULL, actor TEXT, payload JSON
```

- **Event kinds:** `project.created|updated`, `task.created|updated|stage_changed|closed`,
  `task.session_bound|session_unbound`, `report.added`, `decision.added`,
  `session.seen|status_changed|ended|alias_added`, `import.completed`.
- **Description and progress** (migration `0002`): changing either is a `task.updated` with a
  version bump like any field; `report_add` with `progress` writes both events in one transaction.
  Unbinding closes the open `task_session` row and clears `task.session_id`; history is kept.
  Payload carries the changed fields (`{"from": "dev", "to": "test"}`), enough to render a feed line
  and to patch the UI.
- **Tracker mark and aliases** (migration `0003`): the four `tracker_*` fields are ordinary task
  fields (`task.updated`, version bump). A plain name passed to `task_bind_session` for a task bound
  to a desktop session does not rebind: it is stored in `session_alias` (`session.alias_added`) and
  from then on resolves to that session. Aliases are global (one name, one session) and win over
  plain sessions, so no alias is created for a name that a plain session already has; the call then
  only warns.
- **Next-step freshness** (migration `0004`): `next_step_at` is set whenever `next_step` is
  written, `stage_changed_at` on every real stage change (both backfilled from `updated_at`, so an
  upgrade marks nothing stale). A next step with `next_step_at < stage_changed_at` is stale: shown
  muted with a "stale" marker on open cards and in the task dialog, `(stale)` in MCP output. An
  explicit write of the same text confirms it (and counts as a change) when it is stale or the same
  write changes the stage; otherwise an unchanged next step is a no-op as before. The board's edit
  form confirms it only when its text is edited; saving other fields (or the stage) leaves it as is.
- **Archive** is computed, not stored: a done task whose `closed_at` is older than
  `done_window_days` (config, default 7) is left out of the board's done column unless
  "Show archived" or a search is active. With `require_tracker_log = true` (config, default false)
  a done task without `tracker_logged` is never archived and shows "not logged".
- **Stages** are a fixed enum in v1 with a display-label map in config (so a user can show "разработка"
  instead of "dev"). Any transition is allowed (real work jumps back from `test` to `dev`); the event
  log keeps history. Custom stage sets are a v0.2+ question.
- **"Waiting on"** stays free text in v1 with autocomplete from previous values; structured
  assignees are not needed for one user.
- **Retention:** events are kept forever (small); the SSE replay window is the last N=5000 events — a
  client further behind gets a `resync` event and reloads the current view.
- **Concurrency:** `task.version` increments on every write; MCP `task_update` accepts optional
  `expected_version` and fails with a clear message on conflict. Last-write-wins otherwise.

## 4. Live updates and front-end

### Choice: HTMX + SSE (server-rendered), not a SPA

| | HTMX + SSE + Jinja2 | Small SPA (Preact/Svelte + WebSocket) |
|---|---|---|
| Build step | none; one vendored `htmx.min.js` + SSE extension | Node toolchain, bundler, second language in repo |
| Live updates | SSE: auto-reconnect and `Last-Event-ID` built into `EventSource` | WebSocket: reconnect, replay and heartbeats hand-written |
| Contributors | Python-only | Python + TS |
| Rich interactivity (drag-and-drop) | limited, possible with a tiny `Sortable.js` | easy |
| Phone | plain responsive HTML, light | fine, heavier |

**Decision: HTMX + SSE.** The board is mostly read-only views with a few edits; data flows one way
(server → browser), which is exactly SSE. Writes from the browser are ordinary HTMX `POST`s. No Node
toolchain keeps the project one-language and `pipx`-installable. A SPA can be added later behind the
same REST API if the UI outgrows HTMX.

### Live update protocol

- `GET /events?project=<slug>` — `text/event-stream`. Each message: `id: <event.id>`,
  `event: <kind>`, `data: <small JSON>`. Heartbeat comment every 15 s (keeps proxies and phones
  honest).
- On (re)connect the browser sends `Last-Event-ID`; the server replays events with a greater id
  (bounded), or sends `event: resync` if the client is too far behind.
- The page does not receive HTML over SSE; on an event it asks HTMX to refetch the affected fragment
  (`hx-trigger="sse:task.updated[detail.task==12]"` style, or a small 30-line JS dispatcher). This
  keeps rendering in one place (templates) and makes replay idempotent.
- `visibilitychange` / `online` listeners force a reconnect when a phone wakes up — mobile browsers
  silently kill `EventSource` in the background.
- Views: **Projects** (counts per stage, sessions working now), **Project board** (columns by stage,
  cards: title, waiting-on chip, session badge with live status, branch, last report summary and age),
  **Task** (fields, report history, decisions, event feed, rendered linked `TASK.md` read-only),
  **Sessions** (who is working/idle/waiting now), **Feed** (global event stream). Mobile layout:
  columns become a stage tab bar.
- Markdown rendering via `markdown-it-py` with raw HTML disabled (agent output is untrusted).

## 5. MCP server

### Transport: Streamable HTTP inside the main process (primary), stdio bridge (fallback)

- **Primary — Streamable HTTP at `http://127.0.0.1:8765/mcp`.** Many Claude Code sessions (desktop app
  and CLI, on the same machine) connect to the one running server. Writes reach the board instantly
  through the in-process bus. Built with the official Python SDK `mcp` **2.x** (`MCPServer`, formerly
  `FastMCP`; pin `mcp>=2,<3`), mounted into the FastAPI app via `streamable_http_app(...)`; the SDK's
  session manager must run in the app lifespan. The SDK's `TransportSecuritySettings`
  (`allowed_hosts`, `allowed_origins`) gives Host/Origin validation on `/mcp` for free.
- **Fallback — `relaybay mcp-stdio`.** A thin stdio MCP server that forwards each tool call to the HTTP
  API of the running instance (it never opens the database). For clients or environments where HTTP
  MCP config is inconvenient. Same tool set, generated from one definition.
- **Rejected — stdio-only** (each session spawns its own server process writing SQLite): N writer
  processes, no live notification to the board, and Windows file-locking surprises.

### Connecting Claude Code

```bash
claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

or a project/user `.mcp.json` entry:

```json
{
  "mcpServers": {
    "relaybay": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${RELAYBAY_TOKEN}" }
    }
  }
}
```

`user` scope is the recommended default (stored in `~/.claude.json`, one entry for all repositories,
nothing written into the repositories; the desktop app's Code tab reads it too). `.mcp.json` expands
`${VAR}` / `${VAR:-default}` in `url` and `headers`; project-scope servers need a one-time approval
in interactive sessions, none in `claude -p`. A `type` field is mandatory for URL entries. `relaybay token` prints the token and the ready-made command.

### Who is calling

MCP transport sessions do not carry the Claude Code session id. Tools therefore take an optional
`session` argument (a human-readable session name, e.g. the desktop session title); the server
upserts a `session` row by name and binds it. Hooks (§6) supply the real Claude Code `session_id` and
`cwd`, and the server joins them by `cwd → project.repo_path` when both are present. No identity is
trusted for authorization — the token is the only auth; identity is for display.

### Tools (v1)

| Tool | Purpose | Main arguments |
|---|---|---|
| `project_list` | Projects with per-stage counts | — |
| `project_upsert` | Create/rename a project, set repo path | `slug`, `name?`, `repo_path?` |
| `project_snapshot` | Compact markdown snapshot of a project: open tasks by stage, waiting-on, last report per task, recent decisions | `project`, `include_done?` |
| `task_list` | Filtered list | `project?`, `stage?`, `waiting_on?`, `session?` |
| `task_get` | Full task with last N reports and decisions | `task` (key or id) |
| `task_create` | New task | `project`, `title`, `stage?`, `waiting_on?`, `next_step?`, `branch?`, `doc_path?`, `description?`, `progress?`, `tracker_ref?`, `tracker_name?`, `tracker_url?`, `tracker_logged?`, `session?` |
| `task_update` | Patch fields (incl. `description`, `progress`, `tracker_*`); `"-"` or `""` clears a text field | `task`, fields…, `expected_version?` |
| `task_set_stage` | Move stage with a note; optional `next_step` / `waiting_on` saved atomically | `task`, `stage`, `note?`, `next_step?`, `waiting_on?` |
| `task_bind_session` | "Session X works on this task now"; a plain name never replaces a desktop binding, it becomes its alias | `task`, `session` |
| `task_unbind_session` | "Nobody works on this task now"; history kept; `session` guards against unbinding a successor | `task`, `session?` |
| `report_add` | Append a report; first line becomes the card summary; optional `state`/`size` parsed from a leading `state · size · gist` line; optional `progress`, `next_step` and `waiting_on` update the task atomically | `task`, `body`, `progress?`, `next_step?`, `waiting_on?`, `session?` |
| `decision_add` | Record a decision | `project`, `task?`, `text`, `decided_by?` |
| `events_since` | Event feed for catch-up by an agent | `since_id?`, `project?`, `limit?` |

Plus MCP **resources** `relaybay://project/{slug}` (same text as `project_snapshot`) so a session can
`@`-mention a project snapshot. Tool outputs are short, model-friendly markdown, not raw JSON dumps.
Tool descriptions are written so that an agent knows *when* to call them ("call `report_add` at the
end of every stage with your final report"). A copy-paste instruction snippet for a project's
`CLAUDE.md` / `AGENTS.md` ships in `docs/connect-claude-code.md`.

## 6. Session activity without the agent's help (hooks)

The board must show "which session is doing what now" even when the agent forgets to call a tool.
Claude Code hooks fire on session start/end, user prompt, tool use, stop and notification events and
receive JSON with `session_id`, `transcript_path`, `cwd`, `permission_mode` and `hook_event_name`.
Besides `command` hooks Claude Code has a native `http` hook type that POSTs the event JSON to a URL
(headers may reference env vars listed in `allowedEnvVars`; failures do not block the session unless
`onFailure: "block"`).

- relaybay exposes `POST /hooks/claude-code` (token-protected).
- Delivery: native `http` hook pointing at `http://127.0.0.1:8765/hooks/claude-code` with
  `Authorization: Bearer $RELAYBAY_TOKEN` and a short `timeout` (default 600 s is far too long).
  Fallback for older Claude Code: command hook `relaybay hook` (stdlib-only, reads stdin, POSTs with
  a 1 s timeout, always exits 0).
- Hooks and MCP servers from user settings apply both to the CLI and to local sessions in the desktop
  app's Code tab, so one user-scope setup covers both.
- Mapping: `SessionStart` → session `working`; `UserPromptSubmit` / `PreToolUse` → `working` +
  `activity` (tool name); `Notification` → `waiting` (needs the user); `Stop` → `idle`;
  `SessionEnd` → `ended`. `cwd` → project via `repo_path`.
- `relaybay hooks install --scope user` prints (v1) or merges (v0.2) the settings snippet; it never
  edits settings silently.

This is v1-scope and scheduled last (milestone 7): if it slips, it moves to 0.2 and v1 ships with
MCP-reported status only.

## 7. Markdown as data: import (v1) and export (v0.2)

- **Registry import** — `relaybay import registry <file.md> [--project-column ...]`: parses the first
  GitHub-flavoured markdown table with columns *project, task, stage, waiting on, next step, doc*.
  Column headers are matched through a configurable alias map (any language). The stage cell is free
  text; the importer takes the leading stage word through the same alias map (`план`, `разработка`,
  `test`, `live`, `закрыта`, …) and keeps the rest as `stage_note`. Unknown → `plan` + a warning.
  Idempotent: re-import matches by `(project, title)` or an explicit key column and updates, reporting
  a dry-run diff first (`--dry-run` default on the first run).
- **Per-task document** — the doc cell is stored as `doc_path`; the board renders that markdown file
  read-only from disk (only inside configured `docs_roots`). v1 does not parse decisions/logs out of
  free-form task documents — formats vary too much; sections can be imported later by heading name.
- **Project status files** — linked per project as `status_path`, rendered read-only, same rule.
- **Export (v0.2)** — `relaybay export registry` writes the registry table back from the database, so
  markdown readers keep working during transition. Source of truth in v1 is the relaybay database
  (§14, D1).

## 8. Security of a local server

- Bind `127.0.0.1` by default. `--host 0.0.0.0` (LAN, phone) requires an explicit flag and prints a
  warning. For a phone outside the home network the docs recommend a private overlay network or
  reverse proxy with TLS, not exposing the port.
- **Token**: 32 random bytes, generated on first run, stored in the config dir with user-only
  permissions (`0600`; on Windows the user profile ACL), rotatable via `relaybay token --rotate`.
  Required on `/mcp`, `/api`, `/hooks` as `Authorization: Bearer`.
- **Browser**: open `http://127.0.0.1:8765/login?token=…` once (printed at startup); the server sets an
  `HttpOnly`, `SameSite=Strict` cookie and redirects, removing the token from the URL. QR code of that
  URL in the terminal for the phone (LAN mode).
- **DNS rebinding / CSRF**: validate `Host` against an allow-list (`127.0.0.1`, `localhost`, configured
  LAN names) and `Origin` on every non-GET and on `/mcp` (as the MCP transport spec requires — invalid Origin → 403);
  state-changing browser requests also need a CSRF token in the form.
- **Untrusted content**: reports and task documents are rendered with raw HTML disabled; file
  rendering limited to configured roots, resolved paths checked to stay inside them.
- No telemetry, no outbound network calls; front-end assets vendored, no CDN.

## 9. Packaging and cross-platform

- `pyproject.toml` (hatchling), console script `relaybay`, `python -m relaybay`. Runs with `uvx`,
  `pipx`, plain `pip`. Runtime deps (target ≤ 8): `fastapi`, `uvicorn`, `jinja2`, `aiosqlite`,
  `mcp>=2,<3`, `markdown-it-py`, `platformdirs`, `pydantic` (via FastAPI).
- Paths via `platformdirs`: data (`relaybay.sqlite`) in the user data dir, config in the user config
  dir; override with `RELAYBAY_HOME`. All file I/O UTF-8 explicitly (Windows default code pages break
  non-ASCII markdown). `pathlib` everywhere; stored paths normalised with `as_posix()` for display and
  compared case-insensitively on Windows.
- No OS services: the user runs `relaybay` in a terminal; docs show optional autostart (Task Scheduler,
  launchd, systemd user unit) as copy-paste snippets.
- **Docker**: `python:3.12-slim`, non-root user, volume `/data`; inside the container it binds `0.0.0.0`
  but docs publish only `127.0.0.1:8765:8765`. Repository paths for document rendering are mounted
  read-only. Multi-arch image (amd64/arm64) built in CI.
- CI: GitHub Actions matrix Windows/macOS/Linux × Python 3.12/3.13: `ruff`, `pytest`, build wheel,
  smoke-start the server and hit `/healthz` and `/mcp` initialize (smoke step: stage 2).

## 10. Repository layout

```
relaybay/
  README.md  CONTRIBUTING.md  AGENTS.md  CHANGELOG.md  LICENSE  CODE_OF_CONDUCT.md  SECURITY.md
  .github/  ISSUE_TEMPLATE/{bug_report.yml, feature_request.yml, config.yml}  pull_request_template.md
  pyproject.toml  uv.lock  Dockerfile  .github/workflows/ci.yml
  src/relaybay/
    __init__.py  __main__.py  cli.py  config.py  security.py  service.py  events.py
    domain/   models.py  stages.py
    db/       connection.py  repo.py  migrations/0001_init.sql
    web/      routes.py  sse.py  templates/*.html  static/{htmx.min.js, sse.js, app.css, app.js}
    api/      routes.py  hooks.py
    mcp_server/  server.py  tools.py  stdio_bridge.py
    importers/   registry_md.py  markdown_table.py
  tests/   unit/  api/  mcp/  importers/  fixtures/ (demo markdown: acme-shop, demo-api)
  docs/    PLAN.md  data-model.md  mcp-tools.md  connect-claude-code.md  adr/0001-htmx-sse.md ...
```

## 11. Roadmap and estimate

### Delivery stages

The v1 milestones below are delivered in three stages; the project is built first for its author's own
workflow.

- **Stage 1 (0.1.0)** — milestones 1–5 in a minimal form: skeleton and CI on three OSes; data, service
  and event log; token security; MCP server on `/mcp` with the §5 tools; a minimal board (projects
  list, stage columns with cards, live SSE updates, editing a card from the board, phone layout).
  Done when `uv run relaybay serve` starts the server, a Claude Code session connected with
  `claude mcp add` creates a project and a task, changes the stage and adds a report, and the card
  appears/moves on an open board without a reload; `ruff`, `pytest` and CI are green.
  Stage 1 also binds a task to a Claude desktop app session: tools accept the session id
  (`local_<uuid>`) and title; the session row stores `desktop_link` (template
  `claude://claude.ai/epitaxy/<id>`, configurable); a `task_session` table keeps the binding history;
  the card shows the session badge and an "Open in desktop" button (with copy-link fallback), and a
  minimal task page `/t/{key}` shows the binding history and last reports.
- **Stage 2** — milestones 6–9: markdown import, hooks, full event feed and task view, packaging
  (wheel, `uvx`, Docker), community files (§11.1). Also:
  - `SessionEnd` hook marks the session `ended` and unbinds it from its task automatically.
- **Delivered early in 0.1.x** (planned for stage 2, see §3 and §5 for the final shape):
  - *Session binding by name must not replace a desktop binding.* When a task is bound to a desktop
    session (`local_<uuid>`) and `task_bind_session` arrives with a plain name, the desktop binding
    is kept and the name is recorded as an alias of that session (`session_alias`,
    `session.alias_added`) instead of rebinding and losing the "Open in desktop" link; the name then
    resolves to that session in every tool. If a plain session of that name already exists, no
    alias is made and the call warns "already bound to <title> (local_…); pass your local id".
    Tool description: "If you don't know your `local_` id, don't rebind — the coordinator binds
    you."
  - *Done column and archive.* The `done` column shows tasks closed within the last N days
    (`done_window_days`, default 7); older ones move to the archive, reachable through a
    "Show archived" filter and search (by key or title, `?q=`). Done cards hide "waiting on" and
    the next step.
  - *Logged in an external tracker.* Task fields `tracker_ref` (issue key, "ENG-12"),
    `tracker_name` (free text, "Linear"), `tracker_url` (optional http(s) link) and
    `tracker_logged` (bool, "work log entered"). A badge at the very top of the card shows
    "<tracker_name> <tracker_ref>" whenever a key is set (a link with a URL, ✓ when logged).
    Opt-in `require_tracker_log` (default false): done tasks without the mark show "not logged"
    and stay out of the archive until marked. Tracker names are not hard-coded. MCP:
    `task_create` / `task_update` take all four.
  - `"-"` clears a field in `task_update` / `report_add(progress)`: models cannot always send `""`.
- **Stage 3** — agents inside relaybay (the v2 runner below, plus an embedded terminal / Agent Client
  Protocol adapter). The architecture reserves an `agents/` adapter module with one interface
  (`start`, `send`, `stop`, `events`) per backend (`claude -p` stream-json, embedded terminal, ACP);
  no code until stage 3.

### v1 (0.1.0) — board + MCP

| # | Milestone | Done when | Agent-days |
|---|---|---|---|
| 1 | Skeleton | `pyproject`, CLI `serve`, config/platformdirs, `/healthz`, CI matrix green on 3 OSes | 0.5 |
| 2 | Data + service + events | migrations, repositories, service with event append, bus, unit tests | 1.5 |
| 3 | Security | token, cookie login, Host/Origin checks, CSRF, tests | 0.5 |
| 4 | MCP server | `MCPServer` mounted at `/mcp`, 12 tools + resource, stdio bridge, tests through an MCP client; connected from a real Claude Code session | 1.5 |
| 5 | Board | templates for 5 views, SSE endpoint with replay/resync, wake-up reconnect, mobile layout, edits from the board | 2.5 |
| 6 | Import | registry importer with alias maps, dry-run diff, doc rendering from roots, fixtures | 1.0 |
| 7 | Hooks | `/hooks/claude-code`, `relaybay hook`, settings snippet, session status on the board | 0.5 |
| 8 | Packaging + docs | wheel, `uvx` run, Docker image, README quick start, docs pages, CHANGELOG 0.1.0 | 1.0 |
| 9 | Community + repo hygiene (§11.1) | Discussions on, issue/PR templates, labels, CODE_OF_CONDUCT, SECURITY, README badges and "Contributing & feedback", first GitHub Release | 0.5 |
| 10 | Review + fixes | end-to-end pass on Windows and one Unix OS, phone check, buffer | 1.0 |
| | **Total** | | **≈10.5 agent-days** (range 8.5–13.5) |

### 11.1 Open-source presentation (part of v1 "done")

The repository is public, so v1 is done only when it follows common open-source practice. These
files and settings land in stage 2.

- **GitHub Discussions** enabled (`gh repo edit --enable-discussions`) with categories *Ideas*, *Q&A*,
  *Show and tell*.
- **Issue templates** (issue forms): `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`,
  `config.yml` with blank issues off and contact links to Discussions (ideas, questions) and to private
  vulnerability reporting.
- **PR template** `.github/pull_request_template.md`: what/why, how tested, docs + CHANGELOG updated,
  no secrets/personal data.
- **Labels**: `bug`, `enhancement`, `good first issue`, `help wanted` (plus GitHub defaults kept).
- **`CODE_OF_CONDUCT.md`** — Contributor Covenant 2.1; **`SECURITY.md`** — supported versions, report
  via GitHub private vulnerability reporting, response expectations.
- **README**: badges (CI, license, PyPI version), GIF of the live board, quick start, roadmap, and a
  *Contributing & feedback* section — ideas and questions go to Discussions, bugs and accepted features
  to Issues, code via `CONTRIBUTING.md`.
- **Releases**: SemVer tags; each release is a GitHub Release whose notes come from `CHANGELOG.md`
  (Keep a Changelog); later automated by a release workflow that also publishes to PyPI (trusted
  publishing) and GHCR.
- Already in place (2026-11-02): ruleset protecting `main` (PR only, squash, linear history), secret
  scanning + push protection, Dependabot alerts and security updates, private vulnerability reporting,
  read-only `GITHUB_TOKEN`. Required CI status checks are added to the ruleset once CI exists.

### v0.2 — markdown export, richer filters, hook installer, custom stage labels.

### v2 (0.3+) — runner: relaybay starts agents

- `run` table (id, task, cwd, prompt, claude session id, pid, status, cost/duration, started/ended)
  and `run_event` (raw stream-json lines, compressed).
- Start: `asyncio.create_subprocess_exec(shutil.which("claude"), "-p", prompt, "--output-format",
  "stream-json", "--verbose", "--include-partial-messages"?, ...)` in the repository folder, so the repo's own `CLAUDE.md`, skills,
  settings and `.mcp.json` load exactly as for a manual session. On Windows resolve `claude.cmd` /
  `.exe` via `shutil.which`, no `shell=True`.
- Parse JSON lines (`system/init` → session id, `assistant`/`user` → messages and tool use, `result` →
  cost/duration/outcome) into board events; follow-ups with `--resume <session_id>`; cancel = terminate
  the process tree.
- Permissions: since recent versions `-p` starts in a manual mode that rejects calls needing
  approval, so each project profile sets `--permission-mode` and `--allowedTools` explicitly; never
  skip permissions by default. Never pass `--bare`: it does not use the subscription login.
- Stream event types to handle: `system` (`init` with `mcp_servers`, `api_retry`,
  `permission_denied`, hook progress), `assistant`, `user` (subagent messages carry
  `parent_tool_use_id`), `stream_event` (partial), final `result`.
- Headless sessions also receive cross-session messages, so a run can be addressed like any other
  session; relaybay itself does not speak that protocol.
- Concurrency limit per machine and per repo; one run per git worktree.
- The run's own MCP config includes relaybay, so the agent reports through the same tools.
- Interactive sessions keep running in the desktop app/CLI; v2 adds, it does not replace.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Agents forget to call tools → board goes stale | Hooks for activity; tool descriptions + `CLAUDE.md` snippet; "stale" badge when a task has had no event for N days |
| Two sources of truth (markdown and database) during transition | Explicit decision (Q1); export command; import is idempotent with dry-run |
| Subscription terms for headless `claude -p` started by a tool (v2) | Runner only spawns the user's own `claude` binary, never touches tokens; re-check current terms before v2 (§13) |
| MCP SDK / Claude Code config changes (SDK just moved 1.x → 2.x) | Pin `mcp>=2,<3`; CI test against an MCP client; stdio bridge as fallback |
| Desktop app config precedence differs (`claude_desktop_config.json` wins on name clashes) | Use a distinct server name `relaybay`; verify on all three OSes in milestones 4/7 |
| Mobile browsers kill SSE in background | `visibilitychange` reconnect + `Last-Event-ID` replay; resync event |
| Windows specifics: encodings, path case, `claude.cmd`, file locks | UTF-8 everywhere, `pathlib`, single DB writer, Windows in CI |
| LAN mode exposes the board | Off by default, explicit flag, token + cookie, Host allow-list, docs recommend private overlay/TLS proxy |
| Untrusted markdown from agents (XSS) | Raw HTML disabled, CSP header `default-src 'self'` |

## 13. To verify in the docs before/during implementation

- Claude Code MCP: transports, `claude mcp add` flags, scopes and `.mcp.json` (env expansion in
  headers) — <https://code.claude.com/docs/en/mcp>
- Claude Code hooks: events, input JSON, command vs HTTP hooks — <https://code.claude.com/docs/en/hooks>
- Headless / programmatic use: `-p`, `--output-format stream-json`, `--resume` —
  <https://code.claude.com/docs/en/headless>, CLI flags: <https://code.claude.com/docs/en/cli-reference>
- Settings files and precedence (user/project/local) — <https://code.claude.com/docs/en/settings>
- Cross-session messaging (local socket/named pipe, `SendMessage`/`ListAgents`, on by default) —
  <https://code.claude.com/docs/en/cross-session-messaging>; agent teams (experimental) —
  <https://code.claude.com/docs/en/agent-teams>. relaybay does not depend on either; whether it should
  ever read session lists from that channel is a v2 question.
- Desktop app Code tab: which MCP/hook config it loads — <https://code.claude.com/docs/en/desktop>
- MCP Streamable HTTP transport and its security requirements (Origin validation, localhost bind) —
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- Python MCP SDK 2.x (`MCPServer`, `streamable_http_app`, ASGI mounting, what changed from 1.x) —
  <https://py.sdk.modelcontextprotocol.io/run/asgi/>, <https://py.sdk.modelcontextprotocol.io/whats-new/>,
  <https://github.com/modelcontextprotocol/python-sdk>
- HTMX SSE extension — <https://htmx.org/extensions/sse/>; SSE `Last-Event-ID` —
  <https://html.spec.whatwg.org/multipage/server-sent-events.html>
- Terms: subscription OAuth is for ordinary use of Claude Code and native apps; third parties must not
  route requests through a user's plan credentials. relaybay never touches credentials and in v2 only
  starts the user's own unmodified `claude`. Nothing explicit about `claude -p` on a subscription —
  re-check before v2: <https://code.claude.com/docs/en/legal-and-compliance>
- Whether `--verbose` is strictly required with `--output-format stream-json` (all doc examples pass it).

## 14. Decisions (2026-11-02)

1. **Source of truth.** In v1 the relaybay database is the source of truth. Existing markdown registries
   are imported once (re-import is idempotent); `relaybay export` arrives in 0.2. Moving existing
   workflows onto relaybay happens after v1 is accepted.
2. **Port.** `8765`.
3. **Phone access.** LAN mode off by default; enabled only with an explicit flag, token and cookie
   login; no built-in TLS.
4. **Stage labels.** English keys `plan/dev/test/live/done`; display labels configurable (any language)
   in `config.toml`.
5. **Hooks.** In v1, implemented last; if they slip, they move to 0.2.
6. **Names.** Reserve `relaybay` on PyPI and GHCR before the first release.

Architecture decisions are recorded as ADRs (`docs/adr/`); ADR-0001: HTMX + SSE front-end.

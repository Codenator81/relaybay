# Architecture

How relaybay is built. Behaviour is specified in [SPEC.md](SPEC.md); upcoming work is in the
[roadmap](ROADMAP.md) and the [design documents](design/index.md); single decisions are
[ADRs](adr/0001-htmx-sse.md).

## Goal and boundaries

relaybay gives a person who runs many Claude Code sessions across many repositories one live view of
the work: **projects → tasks → stages → reports**, and which session is doing what right now.

- relaybay only listens: sessions call its MCP tools and Claude Code's hooks post events to it.
- It contains no LLM client, no Agent SDK and handles no credentials. Agents are always the ordinary
  `claude` CLI or desktop app under the user's own login. Starting agents is a later step
  ([runner design](design/0012-runner-and-embedded-terminal.md)) and will only start that same CLI.
- Single user, local machine. No accounts, no cloud hosting, no issue-tracker sync.

## Processes

```
 Claude Code sessions (CLI, desktop app)            Browser (desktop / phone)
   │ MCP, Streamable HTTP    │ hooks                    │ HTML (htmx)   ▲ SSE /events
   │                         │ command hook ─► POST     │ POST forms    │
   ▼                         ▼                          ▼               │
 ┌───────────────────────── relaybay process (uvicorn) ─────────────────┴────┐
 │ SecurityMiddleware: Host allow-list, Origin, token / cookie, CSRF, headers │
 │  /mcp    mcp_server ─┐                                                     │
 │  /hooks  web/hooks  ─┼─► Service ──► SQLite (WAL)                          │
 │  /       web/routes ─┘     │  state change + event row, one transaction    │
 │                            ▼                                                │
 │                       EventBus (asyncio, in-process) ──► SSE fan-out        │
 └────────────────────────────────────────────────────────────────────────────┘
        ▲
        │ reads/writes only ~/.claude/settings.json, on an explicit command
   relaybay setup (CLI process, or the Integrations page in the server)
```

- **One process, one writer.** MCP, hooks and the board live in one uvicorn process. Every write goes
  through `Service`, which updates state tables and appends an `event` row in the same `BEGIN
  IMMEDIATE` transaction and publishes the event on the in-process bus after commit. No other
  process opens the database, so there is no cross-process notification and SQLite locking stays
  trivial.
- **Short-lived helper processes** never touch the database: `relaybay token` and `relaybay setup
  --check` talk to the server over HTTP; the command hook (`python -I -m relaybay.hook_client`) is a
  standard-library forwarder started by Claude Code for each hook event.
- **Events are the sync protocol.** `event.id` is monotonic and is the SSE id; a reconnecting browser
  sends `Last-Event-ID` and gets the newer rows replayed. State tables are the source for reads; the
  log is for the feed, catch-up (`events_since`) and audit ("event-sourcing lite", no projections to
  rebuild).

## Modules (`src/relaybay/`)

| Module | Responsibility |
|---|---|
| `__main__.py`, `cli.py` | `relaybay [serve]`, `token`, `setup`, `doctor`, `demo`, `import`, `export`, `hook`; standard-library `argparse` |
| `config.py` | `Settings`: defaults → `config.toml` → `RELAYBAY_*` → flags; data dir via `platformdirs` |
| `app.py` | FastAPI app: lifespan (service start, MCP session manager), routers, static files, middleware; OpenAPI and docs UI disabled |
| `security.py` | Token file, cookie and CSRF values (HMAC of the token), `token_id`, `SecurityMiddleware` (pure ASGI, so streams pass through) |
| `service.py` | Use cases (`create_task`, `set_stage`, `add_report`, `record_hook`, …): validation, rules checks, transaction, event append, publish |
| `events.py` | `EventBus`: one bounded `asyncio.Queue` (256) per subscriber, optional project filter; a full queue drops the subscriber, which then resyncs |
| `db/` | `connection.py` (aiosqlite, WAL, `foreign_keys`, busy timeout, numbered migrations by `user_version`), `repo.py` (SQL), `migrations/*.sql` |
| `domain/` | Models, stages, report parsing, task field list, rules (validate, merge, hash, check), sessions, hooks parsing, environments (access fields, secret checks, levels, proposals), the PreToolUse guard (identifier matching, mutating heuristics, decisions), digests (`digest.py`: gathered data to sections to budgeted text, no I/O), errors |
| `mcp_server/` | `server.py`: `MCPServer` with the instructions, tools and resources, mounted at exactly `/mcp`; `format.py`: markdown output |
| `web/` | `routes.py` (pages with the sidebar, Inbox, Sessions, Activity with the live journal, Stands & access, fragments, forms, comments, `/events`, Integrations), `sse.py`, `hooks.py` (`POST /hooks/claude-code` and the wake-up long poll `…/wait`), `markdown.py`, templates, vendored static assets |
| `machine_setup.py` | `relaybay setup`: detect, plan, marked JSON merge with backup, plan hash, remove, check (server and Claude Code file checks, shared with `doctor`) |
| `doctor.py` | `relaybay doctor`: read-only diagnosis built from the setup checks plus versions, data folder, token and rules hash |
| `demo.py` | `relaybay demo`: seeds the demo workspace through the service layer |
| `markdown_import.py`, `markdown_export.py`, `domain/markdown_layout.py` | `relaybay import` and `export`: the markdown layout, a plan with a hash on a copy of the database, writes through the service, the export manifest |
| `hook_client.py` | The command hook: stdin → POST with 0.3 s timeouts → stdout (a 200 JSON answer only), always exit 0; with `--wait` the wake-up hook (long poll, exit 2 with the comment on stderr) |
| `logs.py` | uvicorn log config that redacts the `/login` query string |

<a id="data-model"></a>

## Data model

```
project       id, slug UNIQUE, name, repo_path, description, task_seq, created_at, updated_at,
              archived_at
task          id, project_id, key UNIQUE, title, stage CHECK(plan|dev|review|test|live|done), stage_note,
              waiting_on, next_step, branch, doc_path, description, progress, session_id,
              priority, version, tracker_ref, tracker_name, tracker_url, tracker_logged,
              next_step_at, stage_changed_at, created_at, updated_at, closed_at, size (S|M|L),
              plan_status (draft|accepted), plan_accepted_at, plan_accepted_by
report        id, task_id, session_id, author, state, size, summary, body_md, created_at
decision      id, task_id, project_id, text, decided_by, created_at, scope (project|task),
              source_kind (report|event|comment|message), source_ref, source_text, valid_from,
              valid_to, recorded_at, superseded_at, superseded_by (decision.id)
document      id, project_id, slug (UNIQUE per project), title, kind, version, created_at,
              updated_at
document_version  id, document_id, version (UNIQUE per document), body, author, reason,
              base_version, section, created_at
memory        id, scope (global|role|project), project_id, role, kind
              (user|feedback|project|reference), title, body, tags, source_kind, source_ref,
              source_text, links, review_at, valid_from, valid_to, recorded_at, updated_at,
              superseded_at, superseded_by (memory.id), last_read_at, read_count, version, author
memory_block  id, scope (global|project), project_id, label, description, body, char_limit,
              owner_only, version, updated_by, updated_at, created_at
              -- UNIQUE (COALESCE(project_id, 0), label)
search_index  FTS5 (kind, ref, project_id, title, body, stems), created at start when SQLite has
              FTS5; rebuilt from the tables when its row count is off
session       id, external_id UNIQUE (local_<uuid>), name (unique among rows without external_id),
              project_id, cwd, kind, desktop_link, status (working|idle|waiting|ended), activity,
              cc_session_id UNIQUE, cc_exact, activity_at, unlinked_reason, last_seen_at,
              created_at
comment       id, task_id, author_kind (owner|agent), author, session_id, body_md, in_reply_to,
              created_at, delivered_at, delivered_to_session_id, read_at, step_id, attachment_id
review_step   id, task_id, position (UNIQUE per task), title, details_md,
              status (open|accepted|rejected), decided_at, decided_by, created_by, created_at,
              updated_at
attachment    id, task_id, step_id, kind (image), stored_name UNIQUE, original_name, mime, size,
              sha256, caption_md, created_by, created_at
              -- the file: <data dir>/attachments/<project>/<task key>/<stored_name>
plan_step     id, task_id, position (UNIQUE per task), title, details_md,
              status (pending|in_progress|done|blocked), done_at, link, outcome, conformance,
              created_by, created_at, updated_at
plan_revision id, task_id, revision (UNIQUE per task), body (JSON: status and steps), author,
              reason, created_at
session_todo  session_id, cc_session_id, item_id, subject, status (pending|in_progress|completed),
              active_form, position, updated_at   -- PK (session_id, item_id)
task_session  id, task_id, session_id, bound_at, unbound_at, bound_by   -- one open row per task
session_alias name PK, session_id, created_at
tracker_entry id, task_id, tracker_name, tracker_ref, tracker_url, logged_at, unlogged_at, created_at
project_rules project_id PK, body (JSON), updated_at
environment   id, project_id, name (UNIQUE per project), kind (local|test|live), base_url,
              sign_in_allowed (CHECK: 0 on live), login_state_path, notes,
              level (none|read|ask|full, NULL = the policy's level), overrides (JSON),
              position, created_at, updated_at, updated_by
environment_access    id, environment_id, kind (ssh|kubectl|db|gitops|logs|monitoring|other),
              fields (JSON, references only; {"value", "test": true} for a test secret),
              created_at, updated_at, updated_by
environment_proposal  id, project_id, change (JSON), reason, proposed_by, session_id,
              status (pending|accepted|rejected), created_at, decided_at, decided_by
policy_layer  id, project_id, environment_id (NULL = the project layer), body (JSON),
              updated_at, updated_by   -- UNIQUE (project_id, COALESCE(environment_id, 0))
policy_proposal       id, scope (global|<project>|<project>/<environment>), project_id, patch
              (JSON), reason, proposed_by, session_id, status, created_at, decided_at, decided_by
event         id AUTOINCREMENT, ts, kind, project_id, task_id, session_id, actor, payload (JSON)
import_item   kind (document|task|task_note|decision|note), item, source_path, target_id,
              content_hash, target_hash, imported_at
reader_cursor reader, level (portfolio|project|task), ref_id (0 = portfolio), last_event_id,
              read_at   -- PRIMARY KEY (reader, level, ref_id)
```

- `task.version` grows on every write; `expected_version` turns a write into a compare-and-set.
- Event payloads carry the changed fields (`{"from": "dev", "to": "test"}`), enough to render a
  feed line and to decide which fragment to refetch.
- The archive is computed from `closed_at` and `done_window_days`, not stored. Events are kept
  forever; SSE replays at most the last 5000.
- Migrations: [SPEC §11](SPEC.md#spec-data).

## Live updates and front-end

Server-rendered Jinja2 + htmx, no build step and no Node toolchain
([ADR-0001](adr/0001-htmx-sse.md)).

- `GET /events` (optionally `?project=<slug>`) streams `id`, `event`, small JSON `data`; every
  page uses the unfiltered stream so the sidebar's Inbox count stays live; heartbeat every 15 s;
  `retry: 3000`. The stream subscribes to the bus *before* reading the backlog, so nothing committed
  in between is lost; duplicates are skipped by id.
- The page never receives HTML over SSE: `app.js` maps an event to the fragment it affects
  (column, card, dialog view, comment thread, Inbox badge and list, sessions list) and asks htmx to refetch it, so
  replay is idempotent; the Activity page asks for the rows newer than its newest one.
- `nav.js` (loaded before the body, not deferred) applies the remembered collapsed sidebar before
  the first paint and runs the phone drawer.
- `visibilitychange` and `online` force a reconnect (phones kill `EventSource` in the background);
  `resync` reloads the page.
- The phone layout switches columns with radio buttons and CSS, without JavaScript.

## MCP transport

- Streamable HTTP at `http://127.0.0.1:8765/mcp` inside the main process, built with the official
  Python SDK `mcp` 2.x (`MCPServer`, `streamable_http_app`). Its routes are added to the app at
  exactly `/mcp` (a mount would redirect `/mcp` to `/mcp/`); the SDK's session manager runs in the
  app lifespan.
- The SDK's `TransportSecuritySettings` get the same Host and Origin allow-lists as the middleware.
- Many sessions (CLI and the desktop app's local sessions) connect to the one server; writes reach
  the board at once through the bus. Registration is `claude mcp add --transport http --scope user
  relaybay …` ([connect guide](connect-claude-code.md)).
- Rejected: one stdio server per session, which would mean many writer processes and no live board.

## Hooks transport

- Default: a `command` hook per event that runs `<python> -I -m relaybay.hook_client --url
  …/hooks/claude-code --token-file …` with the interpreter that runs relaybay (isolated mode, nothing
  on `PATH` needed). It reads the token file itself, so no token or environment variable is in the
  settings file, and it is silent when relaybay is down.
- Optional `http` hooks (`relaybay setup --mode http`): Claude Code posts directly with
  `Authorization: Bearer ${RELAYBAY_TOKEN}` and `allowedEnvVars`.
- `POST /hooks/claude-code` reads at most 256 KB, keeps only session id, event, `cwd`, source, tool
  name and notification message, and answers immediately; `SessionStart` returns
  `additionalContext` with the hook session id so the agent can link itself exactly. Prompt and tool
  events answer with the owner comments for an exactly linked session (`additionalContext`), `Stop`
  with a `decision: "block"` while they are unacknowledged; `PreToolUse` also runs the guard
  (`permissionDecision` `deny` / `ask` or a warning, and a `live.action` event for live) and never
  answers `allow`; the Stop counter and the wake-up
  waiters live in the service's memory (lost on restart, which only means one more reminder).
- `SessionStart` also adds the task context or a short project digest. The answer shares the hook
  client's 0.3 s timeout, so the digests read bounded lists through the read connection and write only
  when a reader's cursor really moves; a failure there is logged and leaves the old answer.
- Opt-in wake-up hook: an `asyncRewake` command hook at `Stop` long-polls
  `POST /hooks/claude-code/wait` (polling the database once a second, up to 25 minutes, one waiter
  per session) and exits 2 with the comment on stderr.

## Security layers

Host allow-list → Origin check → token (`/mcp`, `/hooks`, `/api`) or cookie (board) → CSRF for
cookie writes → security headers (CSP `default-src 'self'`) → untrusted-content rendering. Details,
threat model and non-goals: [SPEC §9](SPEC.md#spec-security).

## Packaging and cross-platform

- `pyproject.toml` (hatchling), console script `relaybay`, `python -m relaybay`. Runtime
  dependencies: `aiosqlite`, `fastapi`, `jinja2`, `markdown-it-py`, `mcp>=2,<3`, `platformdirs`,
  `python-multipart`, `uvicorn`. Development: `httpx`, `pytest`, `pytest-asyncio`, `ruff`; the docs
  site: the optional `docs` group (`mkdocs-material`).
- All file I/O is explicit UTF-8; `pathlib` everywhere; Windows, macOS and Linux run in CI with
  Python 3.12 and 3.13.
- No OS service: the user starts `relaybay serve` (or runs the Docker image). Wheel and sdist,
  `uv tool install` / `uvx` / `pipx`, the Docker image and the tag-driven release workflow follow the
  [packaging design](design/0003-packaging-and-release.md).

## Repository layout

```
README.md  CONTRIBUTING.md  AGENTS.md  CHANGELOG.md  LICENSE  mkdocs.yml  Dockerfile
CODE_OF_CONDUCT.md  SECURITY.md  GOVERNANCE.md
pyproject.toml  uv.lock  .github/workflows/{ci.yml, docs.yml, release.yml}  .github/ISSUE_TEMPLATE/
scripts/        gen_reference.py (docs/reference/*.md) check_dist.py changelog_notes.py record_gif.md
src/relaybay/   cli.py config.py app.py security.py service.py events.py machine_setup.py
                doctor.py demo.py markdown_import.py markdown_export.py hook_client.py logs.py
                db/  domain/  mcp_server/  web/
tests/          one module per area (service, mcp, board, sse, hooks, setup, rules, …), fixtures/
docs/           index.md SPEC.md ROADMAP.md architecture.md mcp-tools.md connect-claude-code.md
                design/ adr/ reference/ getting-started/ how-to/ llms.txt PLAN.md (pointer)
```

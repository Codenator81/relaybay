# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `relaybay serve` (FastAPI + uvicorn, default `127.0.0.1:8765`) and `relaybay token [--rotate]`;
  `python -m relaybay`; settings from `config.toml`, `RELAYBAY_*` environment variables and flags.
- SQLite storage (WAL, numbered migrations) for projects, tasks, reports, decisions, sessions and an
  append-only event log; every write goes through one service layer and publishes an event.
- Token security: Bearer token on `/mcp` (and the reserved `/api` prefix), cookie login for the
  board via `/login`, Host allow-list, Origin checks, CSRF protection for board edits,
  `Content-Security-Policy`.
- MCP server (Streamable HTTP at `/mcp`) with the tools `project_list`, `project_upsert`,
  `project_snapshot`, `task_list`, `task_get`, `task_create`, `task_update`, `task_set_stage`,
  `task_bind_session`, `report_add`, `decision_add`, `events_since` and the resource
  `relaybay://project/{slug}`.
- Board: projects list with per-stage counts, stage columns with cards, card editing, live updates
  over Server-Sent Events with `Last-Event-ID` replay, a stage tab bar on narrow screens, light and
  dark themes. htmx and its SSE extension are vendored.
- Configurable display labels for stages (`[stages.labels]`).
- Sign-in page: unauthenticated browser visits redirect to `/login`, which accepts the access
  token in a form (CSRF-protected) and returns to the requested page.
- `relaybay serve` and `relaybay token` print their data folder; `/healthz` reports a public token
  fingerprint (`token_id`) and `relaybay token` warns when the running server uses a different
  token.
- Report state and size badges on cards and the task page (state coloured by meaning, size S/M/L,
  both with tooltips); labels and tooltips configurable via `[reports.labels]` / `[reports.hints]`.
- Claude desktop app session links: write tools accept a desktop session id (`local_<uuid>`) and a
  `session_title`; cards and the new task page (`/t/{key}`) show "Open in desktop" and a copy-link
  button; the link template is configurable (`desktop_link_template`).
- Session binding history per task (`task_session`), shown on the task page and in `task_get`;
  binding a session emits `task.session_bound`.
- Task `description` and `progress` (markdown, migration `0002` upgrades existing databases in
  place): `task_create` and `task_update` accept both, `report_add` takes an optional `progress`
  saved in the same transaction, `task_get` shows them in full and `project_snapshot` shortened.
- `task_unbind_session` MCP tool and an "Unbind session" button (with confirmation) on the board:
  closes the binding, keeps the history and emits `task.session_unbound`; an optional `session`
  refuses to unbind a session that took over meanwhile.
- Board cards show the first lines of the description, a "Done:" block with the progress and a
  "no session" chip.
- Session aliases (migration `0003`): `task_bind_session` with a plain name on a task bound to a
  desktop session keeps the desktop binding and records the name as that session's alias (event
  `session.alias_added`); the name then resolves to the desktop session in writes, `task_list` and
  `task_unbind_session`. Aliases are global; no alias is made for a name a plain session already
  has (the call warns and changes nothing).
- Done column window and archive: the board's `done` column shows tasks closed within
  `done_window_days` (default 7, `RELAYBAY_DONE_WINDOW_DAYS`); older ones are archived and shown
  with "Show archived" (`?archived=1`). A search box filters cards by key or title (`?q=`) and also
  searches the archive.
- External tracker mark (migration `0003`): task fields `tracker_ref`, `tracker_name`,
  `tracker_url` (http/https only) and `tracker_logged`, accepted by `task_create` / `task_update`,
  shown by `task_get` and `project_snapshot`, edited in the task dialog. Cards show the issue as a
  badge (a link with a URL, ✓ when logged). Opt-in `require_tracker_log`
  (`RELAYBAY_REQUIRE_TRACKER_LOG`, default false): done tasks without the mark show "not logged" and
  are never archived.
- `"-"` clears a text field in `task_update` (and leaves it empty in `task_create`, clears
  `progress` in `report_add`), since models cannot always send an empty string.

### Changed

- Done cards no longer show "waiting on" and the next step; the task dialog shows them muted.
- The done column is no longer cut at 20 cards; it holds the window and is capped at 200 with a
  note.
- Clicking a card opens the task in a dialog over the live board instead of an inline editor: the
  full task (description, progress, reports, session history, desktop link, unbind) and its edit
  form. The dialog has its own URL (`/p/<project>?task=<key>`), closes with Back, Esc or a click
  outside, and is full screen on narrow screens. The task page `/t/<key>` shows the same details.

### Fixed

- Clearing a field in the board's edit form (an empty input) now clears it; it used to keep the old
  value.

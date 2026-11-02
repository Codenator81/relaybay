# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Working rules: the MCP server instructions are a short working contract (snapshot first, pick or
  create a task, keep next step / waiting on / progress current, `state · size · gist` reports,
  `waiting_on` for decisions, `task_patch` for missing fields). Rules as structured data (report
  format, stage requirements `report_since_stage_change` / `field_present`, description template,
  guidance) with built-in defaults (a report before `test`, `progress` before `done`), a global
  `[rules]` table in `config.toml` and per-project overrides in the new `project_rules` table
  (migration `0006`); a config error stops the start, a broken project override is logged and
  ignored (global rules apply, "project override ignored: …" is shown). Read through the tool `rules_get(project?)` and the
  resources `relaybay://rules` and `relaybay://rules/{project}`; `project_snapshot` starts with
  the rules hash. Stage moves that miss a rule succeed with a `Warning (rules)` line in the MCP
  response, the board form and the event's `warnings` (`rules_mode = "warn"`, default), or are
  refused (`rules_mode = "strict"`).
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
- Next step freshness (migration `0004`, upgrades existing databases in place): `task_set_stage`
  and `report_add` accept `next_step` and `waiting_on`, saved in the same transaction (`"-"`
  clears them), and their descriptions ask sessions to keep both current. A next step set before
  the task's last stage change shows muted with a "stale" marker on the card and in the task
  dialog, and with `(stale)` in `task_get` / `project_snapshot`; writing it again over MCP (or editing it on the board) confirms it.
- Tracker history (migration `0005`, upgrades existing databases in place and backfills one entry
  per task with an issue key): a new `tracker_ref` appends an entry and earlier ones stay; marking
  the work log entered or not stamps `logged_at` / `unlogged_at` on the current entry. The task
  dialog and `/t/<key>` show a "Tracker history" block and `task_get` a "Tracker history" section;
  the card badge and `project_snapshot` keep showing only the current issue.
- Automatic unlog: when a logged task gets new work (a report, a changed `progress`, or a stage
  move back out of `live` or `done` to an earlier stage), `tracker_logged` is cleared in the same
  transaction with a `task.tracker_unlogged` event carrying the reason. Metadata edits (title,
  branch, waiting on, next step, tracker fields) keep the mark; an explicit `tracker_logged` in
  the same write wins. A new `tracker_ref` starts as not logged unless `tracker_logged` is passed
  in the same write.
- `task_patch` MCP tool: changes any editable task fields given as one JSON object (`fields`),
  with the same validation, clearing, stage-move and auto-unlog rules as `task_update` /
  `task_set_stage` and optional `expected_version`; unknown names fail with the allowed list. The
  editable fields come from one list that also drives write validation, the new resource
  `relaybay://schema/task` and an "Editable fields: …" line at the end of `task_get`, so clients
  that cached an older `task_update` schema can still set new fields. README and
  `docs/mcp-tools.md` explain schema caching in long-running sessions.
- A test that keeps the repository English-only (no Cyrillic in `src/`, `docs/`, `tests/` and the
  root markdown files).
- `"-"` clears a text field in `task_update` (and leaves it empty in `task_create`, clears
  `progress` in `report_add`), since models cannot always send an empty string.

### Changed

- Docs and tests use English examples for custom stage and report labels; labels in any language
  remain configurable, the defaults are English.

- Cards show only essentials: key, tracker badge (including "not logged") and an icon-only "open
  in desktop" link in the top row, then title, "waiting on", branch and the next step (two lines).
  Description, progress, the last report with its badges, the session name, "no session" and
  "Copy link" moved off the card; the task dialog and `/t/<key>` still show all of them.
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
- A long session title no longer widens a card past its column: the session badge shrinks with an
  ellipsis and shows the full title on hover.

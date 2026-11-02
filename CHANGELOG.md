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

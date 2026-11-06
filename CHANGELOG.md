# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Messages between sessions: `message_send(body, to?, task?, in_reply_to?, ...)`,
  `messages_list(session, box?, unread_only?, task?, limit?)` and `message_ack(message_id?, ...)`. The
  recipient's hooks deliver a message like an owner comment, only on an exact link, comments first,
  framed as coming from another session and not from the owner: it grants no permission. The sender
  is required: a name a live linked session goes by needs its own hook id, another live session's id is refused,
  and a name no hook proved shows as "name not verified" (all clients share one token: this is not
  access control). An unknown or ambiguous recipient is an error; one no hook can reach is said to be
  so. At most 10 sends per sender per minute; a reply deeper than 8 is refused and shown in the Inbox;
  a Stop block once per message. Server instructions, new point 15. Migration `0027_session_messages`
  (additive). The text of a desktop `SendMessage` is still never read.
- Board: a **Messages** page in the sidebar with sent, delivered and read marks and why a message is
  not delivered yet, a Messages block in the task view, an unread count on Sessions rows, and
  **Message loops stopped** in the Inbox.
- Backlog as a queue: an item has a priority (-9 to 9, higher first within a status) and an assignee
  (who it is for). The board's cards have a priority select, a **For** input and a "for <who>" badge,
  and the list a filter by who it is for. **To plan** gives the task the item's priority and makes
  the assignee its `waiting_on`. Sessions change both only on their own new item, like title and
  body; coordinators on any. Migration `0024_backlog_queue` (additive).
- MCP: `backlog_add` and `backlog_update` take `priority` and `assignee`, `backlog_list` takes
  `assignee` (`-` lists the items for nobody), and the new `backlog_patch(item, fields, ...)` changes
  any item field as one JSON object, for clients whose `backlog_update` schema is older.
- `backlog.md` has the columns `priority` and `assignee`. A file without them keeps the stored values.
- End-to-end tests: `tests/e2e/fake_claude.py` stands in for `claude -p`, on PATH behind a shim, with
  a real server, hooks installed by the real `machine_setup`, the real forwarder and MCP through the
  SDK client. Scenarios: task to Inbox, a comment delivered and acknowledged with a Stop block, a
  guard deny on live with its journal line, the spool replay.
- `scripts/probe_claude.py` prints what a real `claude -p` session sees (MCP servers without and with
  `--chrome`); `RELAYBAY_E2E_REAL_CLAUDE=1` runs an opt-in test of the stream shape against the real
  CLI. Neither runs in CI.
- Board: an **Audit** page in the sidebar: who did what on which live stand (time, session, project,
  stand, tool, redacted command, decision, level, rule, task), with filters, paging and live rows. A
  checkbox adds the changes to the policy, the stands and the sessions' switches.
- Sessions: a **No live now** switch on each row (`POST /sessions/{id}/no-live`). While it is on, the
  guard denies that session any touch of a live stand, reads included, whatever the level and the
  guard mode (`off` too), and journals it with the rule `no-live-session`. Events
  `session.no_live_on`, `session.no_live_off`. The switch survives the merge of a guessed hook row
  and an agent's write with another `hook_session`; on a row with no hook link it applies once the
  row is linked, and reusing an ended folder row for a new session clears it.
- MCP: `audit_list(project?, environment?, session?, decision?, days=7, limit=50)`, read-only. There
  is no write tool: the switch is the owner's.
- Migration `0025_session_no_live`: `session.no_live_at`, `session.no_live_by`. Additive only.
- Desktop program: tray notifications, also with the window hidden and no board tab open: a task now
  waiting on you, a task moved to review or test, an agent's reply, an answer from a chat or a
  mailbox, an external server that stopped answering. At most one per 20 seconds: one item with its
  text, several as `3 need you: 2 reviews, 1 reply`, one summary after a downtime. The tray gets
  **Quiet notifications** (no toast, a dot on the icon, one summary on leaving it) and **Open Inbox**.
  `desktop.toml`: `notify`, `notify_quiet`, `notify_kinds`; the kind `permission` (a session waiting
  for you) is available and off by default.
- `GET /api/notifications?after=<id>&limit=100` (Bearer token): the first route under `/api`.
- A brake: **Stop all agents** on `/sessions` and on a project's Sessions page, a banner on every page
  with **Release** (only the owner, on the board), `brake_set` for coordinators and `brake_status`
  over MCP. A covered session is refused its next tool call and its turn ends (`continue: false` and
  `deny`, relaybay's own tools excepted); its `Stop` hook does not block and the owner's prompts get a
  context line instead. It cannot interrupt a running command or a background process, and it fails
  open. `relaybay setup` needs no new run.
- Token budgets per UTC day or in total, for all projects, a project or a task: a **Budgets** block on
  `/usage`, a line in the task dialog, `budget_get` over MCP. They warn at 80% and 100% through an
  event, an Inbox line and one context line per session; a budget set to `stop` uses the brake at 100%.
  They lag the transcript reader. A daily budget's brake ends with the UTC day. Migration
  `0026_brake_budgets` (additive; a task counts from this version on).
- `max_concurrent_sessions` (default 0, off): a `concurrency.over` event per 10 minutes and an Inbox
  line while more sessions run. It stops nothing.
- `doctor` shows `token budgets`; `scripts/hook_timing.py --server` times the `PreToolUse` answer.
- `task_bind_session` and `task_unbind_session` take `caller` (and `task_unbind_session` takes
  `hook_session`): while a brake holds the task or its project only a coordinator changes the
  binding over MCP.
- Server instruction 14 explains a refusal by the brake. An end-to-end scenario covers Stop all and
  Release with the fake `claude`.

## [0.7.1] - 2026-11-06

### Added

- Board: a **Compact** toggle in the tools row, kept per browser (in the desktop program's window,
  until it quits). Compact cards show key, tracker badge, title, the waiting-on chip and the "answer
  received" mark; a chevron on a card expands just that card (kept by task key), a click on the card
  still opens the dialog.
- Task dialog and task page: every block of the Overview (Description, Done so far, Last report,
  Decisions, Comments, Session, ...) collapses under its title. The choice is kept per browser by
  section for all tasks (in the desktop program's window, until it quits); typed text is not lost.

### Changed

- Task dialog and task page: the newest report in full, older ones as an accordion whose summary is
  the report's first line; a live refresh keeps the opened ones.
- `waiting_on` is only who acts next: server instruction 3 and the tool descriptions say so, the
  card's chip cuts it at the first colon (the full text in its title, in full in the task dialog),
  and a write with more than 4 words there gets `Hint: waiting_on is who acts next; put what happens
  into next_step.` in the answer (never a refusal; the value is saved as given).
- The cookie names of the board carry the port (`relaybay_session_8765`, `relaybay_login_csrf_8765`,
  ...), so a server beside another one on the same host no longer signs you out of it. **Browser
  tabs ask for the login link once after this update;** the desktop program's window signs in by
  itself.

### Fixed

- Desktop program: after a cancelled or failed installer, Update works again without a restart
  (it used to answer "already running"). Windows; the program watches the installer process.
- CI: the "Smoke - desktop program" step gives a slow runner 45 s for `/healthz`, then quits the
  program with `relaybay desktop --quit`, so a healthy run stays fast.

## [0.7.0] - 2026-11-06

The plan becomes a living map, review screenshots zoom, hook events survive a server that is away, and the program restarts its own server.

### Added

- Living plan: `plan_step_add(task, title, reason, details?)` appends a pending step without losing
  the plan's acceptance; the step is marked "added after acceptance" (who, when, why) in the Plan
  tab and in the history diff, with a `plan.step_added` event. Migration `0022_plan_step_added`
  (additive).
- Setting `plan_draft_hint_hours` (default 1, 0 off): a task in `dev` whose plan has been a draft
  that long gets a hint line in the coordinator's `portfolio_digest`, `project_digest` and
  `task_context`, and its card shows `draft · N h`.
- Screenshot viewer: zoom from fit up to 8x the natural size with the mouse wheel (around the
  pointer), a pinch, the keys `+` / `-` / `0` (fit) / `1` (100 percent), a double click, or the new
  `-` / percentage / `+` / Fit / 1:1 controls in the bar; drag to pan (the Select tool or one finger;
  the space bar or the middle button under a drawing tool). Markup stays on its spot at every scale,
  pins keep their size on screen.
- Backlog: status `done` for a finding fixed without a task of its own, with an optional link (the
  pull request) and note: **Done** on the board's cards, `backlog_update(status="done", link?)` for
  coordinator sessions, `backlog_list(status="done")`. Done items go to the history ("Planned, done
  and declined") and leave the new counts; `backlog.md` writes `done` or `done: <link>`. Migration
  `0023` rebuilds `backlog_item` (rows kept).
- Desktop program: a watchdog. The embedded server's `/healthz` is checked every 30 s; a dead server
  thread or two failed checks in a row restart it like Restart server, at most 3 times in 10
  minutes, then the problem page and a tray notification say "The server keeps failing: see the
  log". With an external server nothing restarts; the tray tooltip says since when it is
  unreachable.
- `relaybay doctor`: a `hook spool` item, the hook events still waiting for the server.

### Changed

- The stage follows the plan: `plan_step_add`, or reopening a done step, on a task in `review` or
  `test` moves it back to `dev` with the stage note `rework: …`; marking the last step done in
  `dev` with a review checklist set adds a hint to move the task to review.
- Work reported after the plan closed (once a report came after the closing write: a `changed`
  report in any open stage, a `verified` one in `dev` or a new `progress` in `dev`) adds `Warning (rules): the plan
  is closed (N/N done) while work goes on …` (never a refusal); in `dev` the plan reminder says
  the same instead of "all N steps done".
- Server instruction 11 says how to change the plan after a report.

### Fixed

- Review tab: the step's checkbox shows the hand cursor like its label.
- Desktop program: after an update the board window that was open comes back; the Windows installer
  restarts the program with `--background`, which used to leave it in the tray.

- Hook events that carry state (session start and end, a prompt, a notification, the end of a turn,
  a todo change) are no longer lost when the server is slow or down for a moment: the command hook
  retries them once within 2 s and otherwise keeps them in `hook-spool.jsonl` in the config folder
  (200 lines or 256 KB, without the prompt text), which the next run sends first. A replayed event
  older than what the session already has changes nothing, so a late `SessionEnd` never ends a
  session that started again. Tool calls and the guard keep their single 0.3 s try. With an
  external server, update the server together with the program: an older server takes a replayed
  event as a live one.


## [0.6.2] - 2026-11-06

### Added

- Desktop program: a start journal, `desktop-start.log` in the config folder, written before the log
  file is set up: the time, version, command line and working folder of each start and how it ended
  (another instance answered, the lock held without an answer, the extra missing, started, or the
  exception with its traceback) and the exit code. `relaybay doctor` shows the last start as its
  `desktop start` item.

### Fixed

- Desktop program: a start that ended before its log file was set up (for example at login) left no
  trace anywhere, because the windowed program has no console for its messages; it is now in the
  start journal. Troubleshooting has a "relaybay did not start at login" section.
- Backlog page: cards instead of a table, so a long title, the origin and the three actions no
  longer crowd one row; the status filter is as wide as its longest option.
- Stopping the server (Quit, Restart server, Ctrl+C on `relaybay serve`) ends the board's `/events`
  streams first instead of waiting the graceful timeout for them and cutting them with a
  `CancelledError` traceback in the log.
- `backlog.md` import: a title without ASCII words (for example in Cyrillic) no longer gets the
  identity `item`, so two such rows on one day are two items; an item an earlier import tracked
  under that identity is found again, not added twice.
- Desktop program: the downloaded installer is deleted at the next start of the program instead of
  staying in the cache folder; the backup folders before a new version are stamped in local time,
  the same clock as the log.
- CI: the hook forwarder's start-up is timed with `--version`, not with a request whose connect
  could wait for a timeout on Windows.

### Changed

- PyPI: keywords and classifiers (Beta, Developers, Windows, macOS, GTK), so the package is found
  by "claude-code" and "mcp".

## [0.6.1] - 2026-11-05

### Fixed

- Desktop program: the starting and problem pages set their own colours for light and dark, instead
  of dark text on a dark window, and the window background before a page paints follows the machine's
  theme, so a dark machine sees no white flash.
- Desktop program on Windows: the tray menu follows the system theme (dark on a dark machine).
- Desktop program: opening a maximized window from the tray no longer brings it back at the default
  size. The board window's size, position and maximized flag now survive a restart, in
  `window-state.json` beside `desktop.toml`.
- `relaybay setup` in the installed program names `relaybay-hook` as the forwarder instead of "this
  Python".

## [0.6.0] - 2026-11-05

Desktop program: relaybay installs with an installer on Windows, macOS and Linux (macOS and Linux are
beta and not signed), lives in the tray, opens the board in its own window (on Linux in the browser),
starts at login if asked, checks for updates and updates itself, backs the data up before a new
version, and can be a client of a relaybay running elsewhere. `pip`, `uvx` and Docker stay.

### Added

- Desktop program, preview (design 0016): `relaybay desktop` shows the board in a window of the
  system web view with a tray icon (Open board, Open in browser, Restart server, Logs, Settings, Quit)
  and runs the server in the same process; closing the window hides it to the tray. One program per
  user: a second start shows the running window, `relaybay desktop --quit` stops it. Start at login
  (Windows Run key, macOS LaunchAgent, Linux XDG autostart), `--background` starts hidden. Logs go to
  `<data dir>/logs/relaybay.log` (rotating, login token redacted). Needs the optional extra:
  `relaybay[desktop]` (pywebview, pystray, Pillow).
- `desktop.toml` in the per-user config folder (or `RELAYBAY_CONFIG_DIR`), shown and changed with
  `relaybay desktop config` or the program's Settings window: `data_dir` (the data folder when neither
  `--home` nor `RELAYBAY_HOME` is set; nothing is moved), `autostart`, and an external server mode
  (`server_mode = "external"`, `server_url`, token in `external-token`) that the program and the CLI
  (`token`, `hook`, `setup`, `doctor`) use; `relaybay serve` ignores it.
- Entry points `relaybay-hook` (the hook forwarder) and `relaybay-desktop` (windowed).
- An icon: `packaging/icons/` (SVG source, PNG, ICO, ICNS, drawn by `make_icons.py`); the board's
  favicon uses it.
- Frozen builds and installers (design 0016): `packaging/relaybay.spec` (PyInstaller 6.22.3, onedir
  with `relaybay`, `relaybay-desktop` and `relaybay-hook`; `relaybay.app` on macOS), a per-user
  Windows installer (Inno Setup 6, no admin rights, optional start at login, user `PATH`), a macOS
  `.dmg` (arm64, ad-hoc signed, not notarized) and a Linux `.deb` (`/opt/relaybay`,
  `/usr/bin/relaybay`; tray and browser, no window yet). `THIRD_PARTY_LICENSES.txt` lists what the
  builds bundle. CI builds all three on every pull request and smoke-tests the frozen and the installed
  program (`.github/workflows/desktop.yml`); a release attaches them with `SHA256SUMS.txt`.
- Update check (SPEC §13): the server asks GitHub for the latest release 30 s after the start and
  every `update_check_hours` (default 24; no token, only the version in the `User-Agent`), and a
  newer one shows a line in the Inbox with the release notes link and this install's upgrade
  command; `GET /update` returns the state as JSON. `update_check = false` turns it off. The
  desktop program checks its own version too (`update_check`, `update_check_hours` in
  `desktop.toml`): a dot on the tray icon, **Update to X.Y.Z** in the menu and Settings → Check for
  updates. Update downloads this system's installer, runs it only when its SHA-256 matches the
  release's `SHA256SUMS.txt` (integrity, not a signature) and starts it.
- Backup before a new version: when the data folder was last run by another version
  (`last_version`), the server copies the database (SQLite backup API), `config.toml`,
  `policy.json` and `token` into `backups/<old>-to-<new>-<time>/` before it migrates (once per
  step, also for `relaybay import --apply`) and keeps the newest `backup_keep` (default 5).
- The Docker image sets `RELAYBAY_DISTRIBUTION=docker`, so the Inbox suggests `docker pull`.
- Setup from the installed program (SPEC §5, §13): `relaybay setup`, the Integrations page and the
  program write command hooks that start the bundled `relaybay-hook` by its full path; entries of a
  source checkout (`-m relaybay.hook_client`) or another install are replaced in place, an existing
  wake-up hook moves along. Setup refuses while the program runs from a place that goes away (a
  mounted `.dmg`, macOS translocation, the temp or Downloads folder). `relaybay-hook --version`.
- The program checks Claude Code's hooks at each start (read-only) and says in the tray when they
  point at a missing or another install's forwarder; Settings → **Reconnect Claude Code** shows the
  plan and diff of `relaybay setup` and writes it on Apply.
- `relaybay doctor`: the `relaybay` line names the install kind and executable; new items `install`,
  `data folder source`, `server mode`, `hook executable` and `desktop`.
- Docs: install per OS (SmartScreen, Gatekeeper, `apt install` of the `.deb`), how-tos "Move to the
  desktop program" and "Docker server, desktop client" with `examples/compose.yaml`, uninstall per
  OS, ADR-0002 (desktop shell).

### Changed

- SPEC §13 "Desktop program" is no longer a preview; design 0016 is frozen.

- relaybay now makes one outbound call: the update check above (opt-out). It still sends no
  telemetry.

## [0.5.0] - 2026-11-05

Intake: messages from a chat or a mailbox come in through a watcher over MCP, are tied to the task
that waits for an answer and can propose work; one autonomy rule lets a trusted source wake the
bound agent when the owner switched it on, off by default; a backlog per project collects findings
from sessions, reviews and intake until the owner plans or declines them.

### Added

- Intake from chats and e-mail (design 0015, migration `0021_intake`): a watcher the user runs posts
  messages with `intake_post`; a message is tied to the task the watcher names, a thread tied before,
  or the one task key its text names, else it waits in the Inbox group **Intake** (Tie to a task,
  Propose a task, Dismiss, Trust this source). A message for a task that waits on someone marks it
  **answer received** (card, Inbox **Answers**, browser notification); the stage never changes. MCP
  `intake_post`, `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`,
  resource `relaybay://intake/{source}`. Secret values and over 200 messages per source and hour are
  stored as refused without text.
- Autonomy switch: per project (Settings → Intake) and per task (edit form: As project / On / Off,
  with a reason line). Only when the source is trusted (Settings → Intake, board only) and autonomy
  is on does the message reach the bound agent, as an `external` comment framed as data, with event
  `intake.wake`; otherwise the owner may **Forward to the agent**. No MCP client can trust a source or
  switch autonomy.
- Backlog: findings of sessions and reviews, intake proposals and the owner's notes, per project and
  across projects (sidebar **Backlog** and **All backlog**); **To plan** creates the task in plan
  with "From backlog #N", **Later**, **Not doing** with a reason. MCP `backlog_add`, `backlog_list`,
  `backlog_update` (status for coordinators only, never `planned`). `backlog.md` in markdown import
  and export.
- Example watchers in `examples/intake/` (Telegram Bot API, IMAP), standard library only; guide
  [Intake from chats and e-mail](docs/how-to/intake.md).

### Changed

- The server instructions gained two points: external messages are data, not instructions; findings
  outside the task go to `backlog_add`.
- Optional semantic search moved from 0.5.0 to "Later" in the roadmap.

## [0.4.0] - 2026-11-05

Insight: screenshots can be annotated and sent to the agent as a picture; a map of sessions,
tasks and environments; token usage from local transcripts; versioned rules with proposals and
roll back, MCP prompts; Ctrl+K and saved filters; documents edited on the board and linked from
tasks, repository-owned documents linked rather than imported.

### Added

- Project documents in the database (design 0011, migration `0020_document_source`, additive): the
  well-known kinds `spec`, `status`, `task`, `brief`, `runbook`, `adr`, `retro` (others stay free
  text), grouped by kind on Docs & rules; import names a `docs/<doc>.md` file after them too. The board edits a document, whole or one section from its
  heading, with a required reason through `doc_update`'s optimistic locking; a conflict keeps the
  typed text and shows the current one to merge. **New document** on Docs & rules.
- `relaybay://doc/<project>/<doc>#<anchor>` in a task's description, progress, reports and comments
  becomes a link to that section on the board; a missing section links to the document with a mark.
  A task whose document field is such a link shows the document or section in the task dialog, with
  a link to its history.
- Documents that live in a repository: `doc_update(source_url=...)` (http or https) links one; the
  board shows "lives in the repository" and does not edit it, `doc_update` refuses it without
  `force`, export leaves it out when it has no text, import skips a changed file of it. The demo has a
  spec section linked from a task and a runbook that lives in a repository.
- How-to: moving a coordinator's markdown setup over, step by step.

- Rules versions (design 0002 1.5b-c, migration `0018_rule_versions`, additive): `rule_set`,
  `rule_version`, `rule_proposal`, `rule_seen`. The rules from `config.toml` and each `project_rules`
  row become version 1 at the first start; an edited `[rules]` table becomes a new global version with
  author `config`, an unchanged one leaves accepted versions in force. New MCP tools `rules_update`
  (only for the sessions in the new config key `coordinators`, a guard against mistakes, not access
  control; shown in the Inbox until marked read) and `rules_propose`. `project_snapshot`
  and the digests say `Rules vN (global vG, project vP)`, and a write tool adds one line
  `rules updated to vN: re-read relaybay://rules…` once per version and session.
- Settings → Rules: the version per scope, history with author, reason and diff, Accept / Reject for
  proposals (also in the Inbox and the portfolio digest), "Roll back to vK" with a confirmation.
- MCP prompts `start_task` and `finish_stage`: the current rules plus a checklist.
- `report_add` checks the first line against the standard `report_format`: a `Warning (rules)` line,
  refused in `strict` mode. New requirement kind `require_tracker_log`; `require_tracker_log = true`
  adds it to `done`.
- With `require_tracker_log = true` and `rules_mode = "strict"` a move to `done` without the mark is now
  refused; before it was allowed and the task showed "not logged".
- Ctrl+K quick switcher (Cmd+K on macOS, a button in the top bar): fuzzy search over tasks, projects
  and sessions, `GET /switcher`, keyboard and touch.
- Board filters `stage`, `waiting_on` and `session` next to the search, kept in the URL and in live
  updates; saved filters (migration `0019_saved_filters`, additive) with chips on the board and items
  under the project in the sidebar. The demo has a second rules version, an open proposal and a saved
  filter.
- Screenshot annotations (step 11.5b of design 0009, migration `0016_annotations`, additive): the
  viewer has a toolbar with Select, Rectangle, Arrow, Pen, Pin, five colours, Undo, Delete, Hide
  markup and Send to agent, over an SVG layer that scales with the image; it works with touch. Pins
  are numbered per screenshot (numbers are never reused) and open a thread. "Send to agent" flattens
  the screenshot in the browser at its own resolution, uploads the PNG beside the untouched original
  (`POST /files/<id>/annotated`, same CSRF, type and size checks as uploads, counted in the task's
  limit) and posts one owner comment with the pins, their coordinates and
  `relaybay://attachments/<id>/annotated`, tied to the step. New MCP tools `attachment_get` and
  `annotation_reply`, and that resource; `review_get` now lists the pins and returns flattened images
  as image content. The demo has a marked-up screenshot.
- Map (`/map`, `GET /map/graph`, design 0010): the owner, projects, open tasks, sessions and
  environments as a graph, drawn with vendored Cytoscape.js 3.34.4 under the existing CSP. Edges:
  session to task, session to session messages, session to environment coloured by permission level,
  owner comments. Structure and Now (edges of the last N minutes) views, project filter, collapsing
  projects (more than 150 nodes start collapsed), live over SSE, light and dark, phone width. The
  hook receiver records `session.message` for a `PreToolUse` of `SendMessage`: sender, recipient and
  time, never the text, at most once a minute per pair and 20 recipients a minute per sender.
- Usage (`/usage`, migration `0017_usage`, additive): tokens by session and model, per day and week
  and per project, read in the background from the Claude Code transcripts the hooks report.
  Subagents' transcripts count to their session. Incremental by file offset, bounded per pass,
  duplicates counted once, unknown lines skipped and
  counted. Only `usage` numbers and the model name are kept; tokens only, no money or quota figures;
  no network call. New settings `usage_enabled` and `usage_interval_seconds`, new MCP tool
  `usage_summary`, a Usage link on each session row. The demo has environments, messages and a small
  usage history.

### Fixed

- `tracker_url` and the new `source_url` refuse control and invisible characters (NUL,
  bidi overrides, zero-width), so a link cannot read as something other than where it goes.
- The viewer's image no longer collapses on phones, where hidden arrows moved it out of its grid
  cell.

## [0.3.0] - 2026-11-04

Acceptance and memory: a review stage with a checklist and screenshots; versioned documents,
decisions, memory notes and full-text search; a plan with checkpoints on every task; digests per
level and the session's context at start; markdown import and export. Sessions relink by
themselves after `/clear` or a restart, and a comment can wake an idle session.

### Added

- Markdown import and export (step 11.6c of design 0008, migration `0015_import`, additive):
  `relaybay import <root>` brings a markdown layout onto the board (project briefs with their
  decisions, further documents, a task registry with column and stage aliases, per-task notes, memory
  notes, a retro registry). A dry run is the default and prints what would be added, updated,
  unchanged, skipped, in conflict or refused, with diffs and a plan hash; `--apply --expect <hash>`
  writes exactly that plan. A re-run with no changes writes nothing. A target changed on the board
  since its import is a `conflict` (`--overwrite` takes the file as a new version); an edited decision
  bullet supersedes the old decision; an item with a secret is refused by file, line and detector.
  `relaybay export <root>` writes documents, decisions, notes and the registry back, deterministic, with
  a manifest that protects files changed by hand (`--force`). Guide:
  [Import and export a markdown setup](docs/how-to/markdown-import-export.md).
- Digests per level (step 11.6b of design 0008, migration `0014_digests`, additive): MCP tools
  `portfolio_digest`, `project_digest` and `task_context` give markdown index lines with the tool to
  drill down, within a size budget (`digest_budget_chars`, default 12000; per call `budget`, 500 to
  40000; the lowest priority sections lose whole lines first and say `… N more`).
- Reader cursors: with `session`, a digest shows what changed since that reader's last read, grouped
  per task without session noise, and moves on; `since_event` shows from an event and moves nothing;
  otherwise the last 24 hours.
- Project pulse: `project_pulse_set` (at most 5 lines and 600 characters, secret scan, event
  `project.pulse_set`), shown on Docs & rules and the All boards cards with its age and a stale mark
  after `pulse_stale_days` (default 7). A stage move in a project whose pulse is missing or stale adds
  one `Note:` to the MCP answer, once per project per server run. `task_stale_days` (default 5) sets
  when `portfolio_digest` lists a task as without progress.
- Context at the session start: the `SessionStart` answer adds `task_context` for an exactly linked
  session bound to an open task (instead of the plan reminder), or a short `project_digest`
  (`session_context_project_chars`, default 1500) for a session in a project's folder.
  `session_context = "off"` turns it off. Point 1 of the server instructions now starts with the
  digest for your level.
- A nested `claude` that says it runs unattended (`CLAUDE_CODE_SESSION_ATTENDED=0`) never links to
  the desktop session it was started from, by host id or by guess.
- `relaybay demo` sets a current pulse on `acme-shop` and leaves a stale one on `demo-api`; the demo
  block is now `conventions`.
- Task plan and checkpoints (step 11.7, design 0014, migration `0013_plan`, additive): ordered
  plan steps with "what and why" and a status; a done step is a checkpoint with its time, an
  outcome, a link (report or PR) and a required conformance line (`as planned` or `deviation:
  why`). Plans are drafts until the owner (board) or the coordinator (`plan_accept`) accepts
  them; renaming or reordering steps makes an accepted plan a draft again. Every plan write is
  kept as a revision. MCP tools `plan_get`, `plan_set`, `plan_step_update`, `plan_accept`;
  events `plan.set`, `plan.step_updated`, `plan.accepted`, `session.todo_updated`.
- Board: a **Plan** tab in the task dialog (and a Plan section on the task page) with **Accept
  plan**, deviations highlighted, the agent's todo under the current step and the plan's history
  with title diffs; cards show `Plan 2/5 · now: <step>`.
- Task `size` (`S` / `M` / `L`) in `task_update`, `task_patch` and the board form; the first
  report's size sets it when the task has none.
- Rule kind `plan_accepted` (`unless_size`), in the default rules for moves to `dev` (S tasks
  excepted); a move to dev without a plan or todo adds a hint.
- The agent's Claude Code todo (`TaskCreate` / `TaskUpdate`, or `TodoWrite`) is read from
  `PostToolUse` hooks and shown as a second layer, never merged into the plan; plan reminders go
  to exactly linked sessions on start, after a todo change and every `plan_reminder_every` tool
  calls (default 30). The server instructions gain point 11 (start with a plan).
- `relaybay demo` adds an M task with an accepted plan (a deviation included) and an S task with
  an agent todo.
- Memory and documents (step 11.6a of design 0008, migration `0012_memory`, additive): project
  documents with versions, section anchors from headings, section-level writes, optimistic
  locking (a conflict returns the current text to merge) and diffs; decisions with a scope, a
  structured source, time axes and supersede chains (never deleted, no cycles); memory notes
  (global, role or project; user, feedback, project or reference) whose `memory_add` first
  returns similar notes and asks for add / update / supersede / noop; blocks with a character
  limit and an owner-only flag. Writes that hold a secret value are refused, naming the detector.
- Full-text search over documents, decisions, notes, reports and comments: SQLite FTS5 with
  Snowball stems (English, Russian) and diacritics folded, ranked by bm25; without FTS5 it falls
  back to `LIKE`. The index is built at start and rebuilt when it is out of step.
- MCP tools `decision_list`, `memory_add`, `memory_update`, `memory_search`, `memory_get`,
  `doc_list`, `doc_get`, `doc_update`, `doc_history`, `block_get`, `block_set`; `decision_add`
  takes `source` and `supersedes`; resource `relaybay://doc/{project}/{doc}`; events
  `document.updated`, `memory.added`, `memory.updated`, `block.updated`.
- Board: a **Docs & rules** page per project (sidebar, `/p/<slug>/docs`) with search, documents
  (rendered with heading anchors, history, diff between any two versions), decisions with the
  superseded ones collapsed, memory notes, blocks the owner can edit, and the effective rules.
- `relaybay demo` adds a spec with two versions, a superseded decision, two notes and a block.

- Review stage between dev and test: `plan → dev → review → test → live → done` (migration
  `0011_review` rebuilds the `task` table with the wider stage check, keeping every row). The
  board has six columns on two pages on wide screens (plan, dev, review, test | live, done),
  switched with the pager or ← / → (not inside inputs or dialogs); the page is in the URL
  (`?page=2`). Phones keep the stage tab bar, now with six tabs.
- Review checklist with screenshots: the task dialog gets Overview and Review tabs
  (`?tab=review`); each step has an Accept checkbox, an expandable "What, how and why",
  thumbnails with captions and a comment box with Comment / Reject / Accept. A full-screen viewer
  (fit, caption, counter, ← / →, Esc, swipe) opens from a thumbnail. Ctrl+V pastes a screenshot
  into a step. Each decision reaches the bound session as an owner comment ("Step 2 'Login with
  2FA' accepted — continue with step 3"). Cards show "Review 2/5"; the Inbox lists a Review queue
  (tasks in review or test with a checklist, oldest first).
- MCP tools `review_get`, `review_steps_set`, `review_step_update`, `attach_file` and
  `attachment_caption`; moving a task to review or test with an empty checklist adds a hint line;
  the server instructions gain point 10 (write the Owner check into the checklist).
- Screenshot storage under `<data dir>/attachments/`, served at `/files/<id>` behind the board
  cookie with its type, `nosniff` and `inline`. Only PNG, JPEG, WebP and GIF by magic bytes (no
  SVG); paths only from the project's repository, the temp folder or `attachment_roots`, resolved
  before the check, copied under random names; limits `attachments_max_mb` (10) and
  `attachments_task_max_mb` (200); duplicates stored once; an orphan sweep on start.
- Browser notification "Waiting for your acceptance: <task>" when a task moves to review or test,
  with the permission asked only from Settings → Notifications.
- How-to "Screenshots for review": headless Chrome / Edge, Playwright with a saved login state
  (test environments only), Android `adb`, iOS simulator, then `attach_file`; the owner pastes as
  a fallback.
- `relaybay demo` adds a task in review with a three-step checklist and two generated screenshots.
- Session picker in the task dialog: **Bind** a session next to Unbind (the project's sessions
  first); the same binding history and `task.session_bound` event as `task_bind_session`.
- Desktop sessions relink by themselves: the hook passes the Claude desktop app's session id
  (`CLAUDE_CODE_HOST_SESSION_ID`) with `SessionStart`, so after `/clear` or a restart the new Claude
  Code id is linked exactly to the same desktop session and owner comments keep arriving without a
  `task_bind_session`. A live link of another session is never taken. http hooks send it as the
  `X-Relaybay-Host-Session` header (re-run `relaybay setup --apply` to update them).

### Changed

- **Upgrade note:** the default rules now require an accepted plan before a task moves to dev
  (`plan_accepted`, size S exempt). In `warn` mode this is a warning; in `strict` mode moves to dev
  are refused until the plan is accepted or the task's size is S. Opt out per project or globally
  with `stage_requirements.dev = []`.

- `SessionEnd` no longer unbinds desktop sessions, nor any session after `/clear` or `/resume`.
- The wake-up hook is verified with Claude Code 2.1.309: `relaybay setup --wake` works there without
  `--force-wake`, which is now needed only on older or unknown versions.

### Fixed

- Full-page board forms (Stands & access, Settings → Policy, Settings → Integrations without htmx)
  answer a save with POST → 303 → GET and show the outcome once from a short-lived flash cookie,
  so refreshing the page no longer sends the form again.
- Scripts and the stylesheet are linked with `?v=<content hash>`, so browsers stop running a cached
  `app.js` from an older version after an upgrade.

## [0.2.0] - 2026-11-02

Working with sessions: sidebar navigation, Inbox, Sessions and Activity; comments from the board
to agents through hooks; project environments with agent permission levels, a guard and a journal
of actions on live; access and safety rules as a layered JSON policy with presets.

### Added

- Left sidebar on every page: Inbox with a live count, All boards, Activity, Settings, and each
  project with Board and Sessions. Collapsible on wide screens (remembered in the browser), a
  drawer on phones. It replaces the header's Integrations link; `/settings` redirects to
  `/settings/integrations`.
- Inbox (`/inbox`): tasks waiting on you, tasks whose last stage move missed a rule, done tasks not
  logged in the tracker (with `require_tracker_log`) and unlinked hook sessions, updated live.
- `owner_names` setting (`RELAYBAY_OWNER_NAMES`, default `["owner", "me", "you"]`): the
  `waiting_on` values that mean you.
- Sessions pages (`/p/<slug>/sessions`, `/sessions`): hook status, kind, last activity, bound tasks,
  open in desktop and the unlinked reason, updated live.
- Activity (`/activity`): the event feed, newest first, with "Load more", project and kind filters
  and live updates; hook status changes only when that kind is selected.
- Comments from the board to agents: a thread and comment box in the task dialog and on the task
  page, with "sent / delivered / read" marks, live over SSE. The hooks of the bound session (only
  when its agent linked it with `hook_session`) hand new comments to the agent
  (`additionalContext` on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`), and `Stop` blocks the
  end of the turn, at most twice, while they are unacknowledged. Events `comment.added`,
  `comment.delivered`, `comment.read`; migration `0008_comments`.
- MCP tools `comments_list`, `comment_ack` and `comment_add` (agent replies); agent replies show up
  in the Inbox until you mark them read. The server instructions gain point 8 (handle owner
  comments, then `comment_ack`).
- `relaybay setup --wake --force-wake` / `--no-wake`: an opt-in, experimental wake-up hook
  (`asyncRewake`, command mode) that wakes an idle session when an owner comment arrives; not
  verified against a live Claude Code, so `--wake` alone is refused and `doctor` marks it
  `UNVERIFIED`.
- Project environments and access (Stands & access page, `/p/<slug>/environments`, in the
  sidebar): local / test / live environments with base URL, notes, a sign-in flag (never on live)
  and an access map of references only (ssh, kubectl, db, GitOps, logs, monitoring); obvious secret
  values are refused. Agents' permission level per environment and capability (`none`, `read`,
  `ask`, `full`; defaults local/test `full`, live `read`, setting `permission_defaults`), changed
  only on the board, with a confirmation for raising live above read. Migration
  `0009_environments`.
- MCP tool `environments_get` and resource `relaybay://project/{slug}/environments`;
  `project_snapshot` lists the environments; `environment_propose` records a proposal that the
  owner accepts or rejects on the board (listed in the Inbox). The server instructions gain point 9.
- PreToolUse guard (`guard_mode` = `off` / `warn` / `block`, default `warn`, `RELAYBAY_GUARD_MODE`):
  a Bash command or MCP tool input that names an environment gets `permissionDecision` `deny` or
  `ask`, or a warning, by its level and a table of mutating heuristics. A safety net against
  mistakes, not a sandbox; no setup change needed (the existing `PreToolUse` hook is used).
- Journal of actions on live: guarded tool calls that touch a live environment are recorded as
  `live.action` events (tool, redacted command, decision) on the bound task, shown in Activity
  ("Actions on live") and in the task dialog.
- Policy as data: the levels, the guard and the secret checks come from a policy instead of the
  code. Built-in presets `default` (the behaviour so far), `strict` and `trusted`; a global
  `policy.json` in the data directory (setting `policy_file`, `RELAYBAY_POLICY_FILE`, validated
  against a published JSON Schema; a broken file stops the start, later edits are picked up
  without a restart); project and environment layers in the database (migration `0010_policy`).
  Lower layers win; lists replace or use `key+` / `key-`. Every value shows the layer that set it.
- Policy keys: levels per environment kind and capability; guard mode `off` / `log` / `warn` /
  `block` (`log`: live journal only); built-in guard rules and secret detectors with stable ids to
  switch off; custom guard patterns (validated regular expressions); allow patterns for secret
  checks; test secrets on local and test environments (`{"value": "...", "test": true}` in access
  fields, masked on the board, not sent to agents unless `expose_test_secrets_to_agents`, masked
  in the journal, never on live).
- Settings → Policy (`/settings/policy`): effective policy per scope with sources, preset picker,
  JSON editor per layer with validation preview, confirmation when a change widens access on
  live, agent proposals. Stands & access shows where each level comes from and a "Test secrets"
  box when the policy allows them.
- MCP tools `policy_get` (effective policy with the source of every value) and `policy_propose`
  (a proposal in the Inbox and on Settings → Policy).
- Docs: [policy reference](docs/reference/policy.md) generated from the schema, how-to "Full
  access on a trusted local machine", design 0013.
- Dependencies: `jsonschema` (already installed through `mcp`) validates policy layers; `regex`
  runs the policy's custom patterns with a hard time limit, which Python's `re` cannot do.

### Changed

- `permission_defaults` and `guard_mode` in `config.toml` are now a legacy policy layer under
  `policy.json` (they still work); `guard_mode` also accepts `log`.
- Secret refusals name the detector that matched, e.g. `notes looks like it contains a secret
  value (jwt)`.

- Board and task pages subscribe to the unfiltered `/events` stream (for the Inbox count) and keep
  only their own project's events.
- `events_since` also describes `session.seen` and `session.status_changed` events.
- `relaybay setup` also installs a `PostToolUse` hook; run `relaybay setup --apply` again after
  upgrading (the check shows "some events are missing" until then).
- The command hook prints the server's answer only when it is a `200` with a JSON object.
- `hook_session` no longer moves an id that another live session linked itself (the write answers
  with a warning line), and `task_bind_session` ignores `hook_session` (pass it on your own writes).

## [0.1.0] - 2026-11-02

First public release: a local board and MCP server for many Claude Code sessions, installable with
one command (`uv tool install relaybay`, `uvx relaybay`, `pipx install relaybay` or the Docker
image), with Claude Code hooks for live session status, working rules served to every session,
`relaybay setup` and `relaybay doctor` for the machine setup, and a docs site.

### Added

- Packaging and release: version `0.1.0` in `pyproject.toml` (the only place; `relaybay
  --version` reads the installed metadata), wheel and sdist with the templates, static assets and
  migrations, a `Dockerfile` (`python:3.12-slim`, non-root, `/data` volume) and a release workflow
  on `v*` tags: build and check the artifacts, publish to PyPI by trusted publishing, push the
  multi-arch image `ghcr.io/codenator81/relaybay:<version>` and `:latest`, create the GitHub
  Release with this file's notes and deploy the docs.
- `relaybay doctor [--json]`: relaybay, Python and OS versions, data folder and token file, Claude
  Code and its minimum supported version (2.0.0), the desktop app, the server with the same token,
  MCP `initialize`, the hook endpoint, the rules hash against the running server, the hooks in the
  user settings and the MCP registration. Read-only (it never creates the token); reports the
  Claude Code files even when the server is down; the output has the shape of `setup --check
  --json` and the exit code is 1 when an item fails.
- `relaybay demo --home <dir> [--force]`: seeds a neutral demo workspace (`acme-shop`, `demo-api`:
  tasks in every stage with descriptions, progress and reports, tracker badges, a stale next step,
  a decision, a desktop session shown as working). A folder that already holds a board is refused;
  `--force` renames its database aside first.
- `/healthz` also returns `rules_hash`, the global rules the server started with.
- Docs: install on Windows, macOS and Linux (uv, uvx, pipx, Docker), "Install relaybay with your
  coding agent" (an idempotent procedure and a copy-paste prompt), CLI, configuration and MCP
  reference pages generated by `scripts/gen_reference.py` (CI fails when they drift).
- Community files: code of conduct (Contributor Covenant 2.1), security policy, governance note,
  issue forms and a pull request template.
- Live session status from Claude Code hooks: `POST /hooks/claude-code` (Bearer token, same
  Host/Origin rules as `/mcp`) maps `SessionStart` / `UserPromptSubmit` / `PreToolUse` to working
  (with the tool name as activity), `Notification` to waiting, `Stop` to idle and `SessionEnd` to
  ended, which also unbinds the session from its tasks (`task.session_unbound`, reason
  `session_end`). Activity is coalesced to one `session.status_changed` event per session per 5 s.
  Cards show a status dot on the "open in desktop" icon; the task dialog shows the status and the
  last activity. Prompt text and tool input are never stored.
- Exact linking of hook sessions: the `SessionStart` answer tells the agent its hook session id,
  and every write tool (and `task_bind_session`) accepts `hook_session`; the MCP instructions say
  so. Without it, a hook session is linked by its working directory to the one desktop session bound
  in that project; ambiguous cases stay unlinked and are listed on the Integrations page. Migration
  `0007` adds `session.cc_session_id`, `activity_at` and `unlinked_reason`.
- `relaybay setup` (`--dry-run` default, `--apply`, `--remove`, `--check [--json]`, `--expect
  <plan hash>`, `--mode command|http` with `command` as the default, `--claude-home`): detects
  `claude` and its version and the desktop app, prints the exact diff of `~/.claude/settings.json`
  and its plan hash, backs the file up before any write (through a symlink to its target), adds or
  removes only relaybay's own hook entries (a command hook running `relaybay.hook_client` with
  `--url …/hooks/claude-code`, or an http hook posting exactly to `/hooks/claude-code` on a
  loopback or the configured host), never writes the token, refuses files that are not UTF-8 or
  valid JSON, and prints the `claude mcp add` command instead of editing `~/.claude.json`.
  `--check` also runs the hook's interpreter once. `relaybay hook` and
  `python -m relaybay.hook_client`: a standard-library command hook that gives up after 0.3 s and
  always exits 0.
- Board: **Integrations** page (header link) with detection, the plan, Apply / Remove / Run check
  buttons (Apply and Remove write only the plan shown) and the unlinked hook sessions. Hook input
  over 256 KB is dropped quietly (`204`). `relaybay serve --claude-home` / `RELAYBAY_CLAUDE_HOME`.
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

- Documentation restructured: `docs/SPEC.md` (shipped behaviour with stable `#spec-…` anchors),
  `docs/ROADMAP.md` (versions 0.1.0 to 1.0 with status), `docs/architecture.md`, one design per
  upcoming step in `docs/design/`, configuration and CLI reference pages, `docs/llms.txt`; the
  former `docs/PLAN.md` is now a pointer to them. A MkDocs Material site (`mkdocs.yml`, optional
  `docs` dependency group) is built with `mkdocs build --strict` on pull requests and deployed to
  GitHub Pages from `main`.
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

[Unreleased]: https://github.com/Codenator81/relaybay/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/Codenator81/relaybay/releases/tag/v0.7.1
[0.7.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.7.0
[0.6.2]: https://github.com/Codenator81/relaybay/releases/tag/v0.6.2
[0.6.1]: https://github.com/Codenator81/relaybay/releases/tag/v0.6.1
[0.6.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.6.0
[0.5.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.5.0
[0.4.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.4.0
[0.3.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.3.0
[0.2.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.2.0
[0.1.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.1.0

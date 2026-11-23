# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- MCP answers give the board link of a document or section for a person to open, `http://127.0.0.1:8765/p/<project>/docs/<doc>#<anchor>`, next to the `relaybay://doc` names: `doc_list`, `doc_outline` (one line), `doc_get`, `doc_search` hits, the document pointers of `task_context` and `task_doc_link`, and the `doc` field of `task_get`. The address comes from `host` and `port`, or from the new setting `board_url` (`RELAYBAY_BOARD_URL`) for a LAN name.
- A card whose `doc_path` is a local `.md` file gets that file as its file document `task-<key>` (read only, writes through relaybay off) on `task_create`, `task_update` or `task_patch`; the answer names it with its board link, or says why not.
- Card fields through `task_patch` and the board form (migration 0060): `deploy_notes` (what a deploy needs: migration, flag, script, rollback) with `deploy_done`, which a change of the notes clears unless it is passed too; `cancelled` (the reason; setting it stamps the time, `-` clears both; the card is not moved, the answer says so). `task_context`, `task_get` and the board show them, the card gets a `cancelled` chip, and a `deploy` approval request shows the card's deploy notes in its answer and on the board.
- `description_add` in `task_patch` appends a paragraph to the description instead of replacing it.
- A worktree per linked repository: the hooks store the folder a bound session works in, and a `repos` entry `name <absolute path>` sets it by hand. `task_get`, `task_context` and the board's Code row (with its own Open in VS Code icon) show it.
- `review_step_update(remark=...)` adds the session's comment under the step on the board without changing its status.
- Blockers between cards (migration 0061): `blocked_by` in `task_update` and `task_patch` takes card keys of any project (`-key` removes one); an unknown key, the card itself and a cycle are refused. A blocker blocks while it is in plan, dev, review or test. `task_context` and `task_get` list the blockers with their stage and board link and the cards a card blocks; the board shows a `blocked by <key> · <stage>` chip and Blocked by / Blocks rows in the task dialog. The archive purge exports the links of a deleted card.
- Merge order of the cards in `test`: after the test cards that block them, otherwise by the owner's order of the column, with warnings for a repository two test cards share and for a blocker not yet in test. `project_digest` and `project_snapshot` show it as `## Merge order (test)`, and the board's test column as a **Merge order** fold, when two or more cards are in test.
- Deleting a project document: `doc_update(delete=true)` at the current `base_version` with a reason, and **Delete document** on the document page with the name typed again. Documents of kind `spec` and `status` only for the owner or a coordinator session, a change document `spec-<task key>` also for the session bound to that task. An export (`deleted/<time>-<project>-doc-<name>.json`) is written first; a file document's file stays on disk. Event `document.deleted`.
- `task_get`, `task_context` and the other card tools answer for a key the archive purge deleted with the date and its export file instead of "not found".
- Change documents: the document `spec-<task key>` is that card's change of the main spec before deploy, `## <doc>#<anchor>` per section with `ADDED:`, `MODIFIED:` (with an optional `Was:` line) or `REMOVED:` blocks. A write of one answers its format problems as a `Warning:` line. While the card is open (not done, not cancelled) the sections it names, and the sections linked to the card, show `changing on <stage>: <key> · spec-<key>` under their heading on the document page and in `doc_get`, and ` · changing: <key>` in `doc_outline`. `task_context` gets a `## Change document` section with the targets, problems and other cards changing the same sections. A move to done hints at a change document still there and at deploy notes not done (not for a cancelled card).

### Changed

- A card's document field holding the path of a file that a file document of the project reads leads to that document on the board and in `task_context`, like a `relaybay://doc` link.
- GitHub-style anchors (`#2-kyc--kyb--veriff`) find the same section as relaybay's (`#2-kyc-kyb-veriff`) in `doc_get`, document links and the document page, so links written inside a document for GitHub land on their heading.
- The task dialog's Overview and the task page put what waits for you first: open approvals and questions, unread agent replies, then Next (next step, waiting on, blockers, cancelled), the plan's steps, the Last report (now a fold like its neighbours), deploy notes, description and progress; task details, the session, decisions, findings, the document and the histories come folded at the bottom. A done or archived card leads with its last report and code. Blocks with nothing in them are left out instead of showing "No reports yet" and the like; a card without comments folds the comment box into Add comment.
- Docs & rules marks a file document whose file is gone with "file missing since ..." instead of "missing".

### Fixed

- A report on a card in `live` or `done` no longer clears the tracker's work-log mark; a move back to an earlier stage still does.
- A section opened by its anchor is no longer hidden under the sticky top bar, and signing in keeps the `#anchor` of the link that led to the sign-in page.
- The note about a file document changed outside relaybay names only sections the file still has; removed ones are listed apart (`removed checkout`) and named again when they come back.
- The secret scan no longer refuses a URL parameter without a value (`/admin-invitation?token=`, `?token=&next=1`, `token=<token>`); a parameter with a value is still refused.

## [0.15.2] - 2026-11-23

### Changed

- Update from the tray shows how far it got: the notification gives the installer's size, the tray tooltip shows `Downloading X.Y.Z: 12 of 39 MB (30 %)`, the menu the same in 10 % steps and then `Checking the download...`, and **Cancel update** stops the download, also during the check (the file is deleted, nothing runs). Tray notifications longer than Windows takes are cut. A failed download says that nothing was changed and that Update tries again. The installer and the backup before a new version are unchanged.

### Removed

- Migration 0058 drops the tables of the runs inside the program, unread and unwritten since 0.12: `run`, `run_turn`, `run_event`, `run_worktree`, `run_profile`, `project_run_defaults`, `coordinator_wake`, `schedule_run`. Questions and approvals keep every row; their `run_id` and `turn_id` stay as plain numbers. The program backs the database up before the new version migrates; going back to an older release means restoring that backup.
- Migration 0059 drops the rest of what 0.12 left unread: `schedule`, the usage and budget tables (`usage_file`, `usage_day`, `usage_message`, `usage_task_day`, `token_budget`, `budget_mark`), `brake`, `session_message`, the intake tables (`intake_source`, `intake_message`, `intake_thread`) and the columns `project.autonomy`, `project.run_retention`, `session.transcript_path`, `task.autonomy`, `task.answer_received_at`, `task.answer_message_id`. Tasks and backlog items keep every other value; a backlog item's intake message number stays. Backup and rollback as for 0058.

## [0.15.1] - 2026-11-20

### Changed

- The Code row of a card shows only the repositories the task linked: no folded **Repos: all** line and no `Code: all repos` count any more; `task_get` names the project's repositories instead and asks the session to link the ones it touches. A pull request tied to no project repository is named by the repository in its link (`mobile #7`), so two of them no longer read "PR PR". **Open in VS Code** is a `</>` icon with the path on hover.
- A task in two repositories shows a branch in each: the hooks record the branch checked out in the repository a bound session works in (migration 0057 adds `task_repo.branch`), the card shows it on that repository's row instead of one branch chip, and `task_get` names it per repository.
- A report's `verified:` marks are no longer listed a second time with ticks under its text.
- Backlog on the board: the **new** mark means "not seen yet" and goes once the item has been fully on screen (`POST /backlog/seen`, migration 0056 adds `backlog_item.seen_at`; items decided before count as seen). The status badge shows only when the status is not `new`.

### Fixed

- The dismiss cross of a board notice ("Warning (rules): …") sits inside the notice, centred on it.
- A plan accepted over MCP by a session named `owner` or `board` is recorded as that session's acceptance (`session owner`), not as one on the board.
- The Done and Not doing menus of a backlog item float over the card instead of stretching the actions row (a card rule made every form in it `position: relative`).
- The secret check no longer takes prose such as "the basic callback" for an auth header: after `bearer`, `basic` or `digest` the value must look like a credential (a digit, a token symbol or a capital after its first letter); after `Authorization:` any value still counts. A section write into a spec that says "basic callback" was refused.
- `relaybay import --backlog`: a cell holding only a dash (`-`, en or em dash) is empty, so it is no longer stored as a triage note; an empty decision cell on a row imported before keeps the item's status on the board (later, done, declined) instead of moving it back to new.

## [0.15.0] - 2026-11-20

### Added

- `relaybay import --backlog <file> --project <slug>` (design 0027) reads only the rows of one project, the others are skipped as "other project". A row that is an open item of that project already on the board is not created again: it matches when its text names the item (`#N`, `backlog #N`, never `PR #N`), its title is alike (title similarity 0.6 and up) or it holds nearly all of the item's title words (0.7 and up); the dry run shows `matches #N: names it` / `title 0.71` / `cover 0.80`, and `--apply` records the row against the item without changing it, so a second import is a no-op. A row near an item (0.4 up to the threshold, or whose item a better row took) is created with a `near #N (score)` warning. Matches count in the plan hash.
- `relaybay import --match LINE=N` (repeatable, with `--backlog` and `--project`): the registry row on line LINE is item N (`matches #N: by hand`), over the automatic matching; `--match LINE=new` creates the row although it matches. An item that is missing, of another project, not open or already another row's, or a line that is no row, is an error line in the plan; the choices count in the plan hash.
- File documents written through relaybay (design 0027, part A), opt-in per document: `doc_update(file_writes="on"|"off")` (MCP, additive; coordinator sessions only, at the add or later) turns writes through relaybay into the file on or off; they are off by default and for every existing file document. With writes on, `doc_update` with `section`, `lines` + `expected` or the whole text writes into the file: the change is spliced into the bytes so only the replaced lines change (each other line keeps its line ending; the byte order mark and the final newlines stay), the new text is checked for secrets, the path rules are checked again, the result is at most 8 MB, and the file is replaced atomically through a temporary file in the same folder after a second hash check (changed in between: nothing is written, a conflict). One `document.updated` event names the changed sections.
- A change made to a file document's file outside relaybay is named: the read that sees it writes `document.updated` with `outside: true` and the changed anchors, and `doc_outline`, `doc_get`, `doc_search` hits and the linked-section lines of `task_context` add `Note: <doc> was changed outside relaybay at <time>: sections a, b (version N).` until the next write through relaybay. Migration 0054 adds `document.file_changed_by`, `file_changed_at`, `file_changed_sections` and `file_writable` (additive).
- Sections linked to a card (design 0027): the MCP tool `task_doc_link(task, doc, section, remove?)` links a section of a project document to a card, checked against the document's current outline (a file document is read first), and answers with its pointer line; `remove` unlinks it. A section that disappears later stays listed as broken. `task_create(sections)` (a list of `doc#anchor`, additive) links them when the card is created; `task_get` and the card page list them. Migration 0055 adds `task_doc_section`.
- `task_context` gains **Open backlog items**: the open items of the card's project that are linked to the card, whose area meets the card's subject (word stems of its area, its linked sections and its title), or that point inside a linked section (a `path:line` in its lines, or the section's anchor or number); at most 8, the rest counted.
- `decision_add(area)` (additive, migration 0055 adds `decision.area`); `decision_list` shows it.
- Deleting from the archive. A task archived by hand (or from live) more than `archive_purge_days` ago (new setting, default 30, `RELAYBAY_ARCHIVE_PURGE_DAYS`, 0 never) is deleted by the server at its start and once a day, with its reports, comments, review steps, plan, attachments (the files too) and task decisions; backlog items it was linked to stay with the link cleared, its events stay without the task link. Event `task.purged`. A task the done window archived by age is never deleted.
- **Delete project** on the Settings page of an archived project, next to Archive (type the slug to confirm) and the MCP tool `project_delete(slug, confirm)` for coordinator sessions only (additive, contract 2): deletes the project with its tasks, documents (the rows only, files on disk stay), backlog, decisions, notes, areas and events; a project not in the archive is always refused. Event `project.deleted`.
- Before each of these deletions relaybay writes an export of the deleted rows to `deleted/<UTC time>-<project>[-<task key>].json` in the data directory, or a `.zip` with the attachment files; a failed write deletes nothing.

### Fixed

- The command line no longer fails with `UnicodeEncodeError` when its output goes to a file or a pipe on Windows: stdout and stderr in another encoding print UTF-8.

### Changed

- The task's comments sit right above the comment box and read like a messenger (yours on the right, the agent's on the left), so a sent comment shows at once; comment numbers and the session id it was delivered to are gone, an agent without a readable name is "Agent", and comments on review steps stay in the step's thread on the Review tab (the task thread counts them).
- A review step shows its conversation like a messenger: the owner's comments and decisions on the step on the right, the agent's replies on the left, oldest first, each with its time (and sent / delivered / read for the owner's); it refreshes live when a comment or a reply arrives. A step title that already starts with its own number ("5. ...") shows the number once.
- The server's instructions to sessions are shorter and ordered by when they apply (at the start, while you work, at the end, always): every card gets an area (reuse the project's, add one only when none fits), every task gets a plan and a Claude Code todo, finished work goes to done, and review is only for a step-by-step acceptance the owner wants.
- "Show archived" on a project board shows the archived cards in their own **Archive** column after done (page 2 on wide screens, an Archive tab on phones), newest archived first, with the board's filters applied, instead of mixing them into done; done keeps only the cards not in the archive. The column is refreshed live (`GET /p/<project>/col/archive`), is no drop target, and a card leaves it by **Back from the archive**.
- The task dialog keeps its height when you switch between Overview, Plan and Review: a short or empty tab no longer shrinks the window to one line, and every tab has a minimum height. An empty Plan or Review tab now speaks to the owner: what will appear there, who writes it and when, and what to do now: **Ask for a plan** or **Ask for review steps** sends the bound session a comment, and a card without a session points to Copy the brief.
- A card waiting on the owner shows it once, as the state word **Waits for you**; the yellow "waiting on" chip stays only for someone else (a person, a role, another session).
- The Plan tab shows the bound session's own todo (its Claude Code task list) open under the plan, each item with its status and when it last changed; with no plan, the todo stands alone under "No plan yet".
- A file document's `version` counts the hashes of its file: 1 when added, one more for every write through relaybay and every outside change a read notices (migration 0054 moves 0.14 file documents from 0 to 1, since `base_version` 0 means "create"). `doc_get` and `doc_outline` of a file document name the version and whether writes through relaybay are on; with writes off `doc_update` is still refused, and the refusal says so.
- **Archive project** and **Back from the archive** moved, with the repositories and the areas, to the project's new **Settings** page (`/p/<project>/settings`, in the sidebar under each project); the old `/p/<project>/environments` redirects there.
- `task_context` lists the sections linked to the card first in "Linked document sections", keeps the card's own decisions in "Decisions" and lists in "Related project decisions" only the project decisions related to the card (its area, a linked section named in the text, or two stems of its subject); the others are counted with the `decision_list` call. Over the budget the backlog items go first, then the related decisions, then the linked sections after the first.
- Archiving a task is no longer forever by default: set `archive_purge_days = 0` to keep archived tasks.

### Removed

- The Stands & access page: the environment and access forms, agents' levels per environment, the saved sign-in, the per capability levels and the environment proposals with their Inbox section, since hosts and access now live in each project's own notes. Nothing is deleted from the database; environments stored before still drive the guard, the approvals and the live journal, and the Audit page still shows their events.
- `environments_get`, `environment_propose` and the resource `relaybay://project/{slug}/environments` do nothing: they stay registered with their parameters within MCP contract 2 and answer `Nothing done: this tool does nothing since 0.15.` with a `Deprecated: …` line, until the first later minor release from 2026-12-19. The MCP instructions, `project_snapshot`, `project_digest` and `task_context` no longer point to them.

## [0.14.0] - 2026-11-19

### Added

- The backlog as a bug registry (design 0026). An item has a kind (`bug`, `idea`, `question`, `debt`; none for older items), a code location (`repo:path:line`, `path:line` or a link), the source in words, a triage note, the item it duplicates and the day it was found (migration 0051, additive). `backlog_add` takes `kind` and `location` and names open items with a similar title (`Note: similar open items: #12 (0.71) …`). `backlog_update` and `backlog_patch` take `kind`, `location`, `triage_note`, `duplicate_of` (declined as "duplicate of #N") and `task` (links an existing card: the item becomes planned); the last three are for coordinator sessions only. `backlog_list` takes `query`, `kind`, `since`, `task` and `has_location`. Backlog items join the search index as kind `backlog` (`memory_search`, the Docs & rules search). The Backlog page has a search box, a kind filter and shows the kind, the location, the triage note and a link to the item a duplicate repeats.
- `relaybay import --backlog <file>` reads a bug registry under any name, without a folder or in place of its `backlog.md`, and stops refusing real rows: a long what is the body with a title made from its first sentence, a `dd.mm` date without a year is the latest such day not after the file's modification time, a project matches by name too, a source outside the four origins is kept as text, the first `path:line` of the what is the location, a decision outside the vocabulary (and the new `decisions` aliases) leaves the item new with the text as its triage note, a too long area, source or note is cut with a warning, and a line break typed into a cell or rows after a blank line keep their rows. The plan counts the rows read per file. `backlog.md` gains the columns kind, location and note; export writes them and an import of the export changes nothing.
- A live card can be archived (design 0026): **Archive** on a card in `live` (card menu and task view, one confirmation) closes it and archives it in one step, with no rule checked; **Back from the archive** returns it to `live` with its work-log mark. A live card with an open question, an open approval request or an approved, unused approval is refused, so nothing is closed for good. `task_archive` and `task_unarchive` take a live card too. Migration 0052 adds `task.archived_from`; after a downgrade and a new upgrade a card moved on in between may keep a stale `archived_from` (Back from the archive of a card the done window archived then returns it to live).
- **Archive all shown (n)** under the cards of the done column archives the cards on screen (the board's filters apply) in one transaction; the notice's **Undo** brings back exactly those cards. The `task.archived` and `task.unarchived` events carry one `batch` id.
- Projects in the archive: **Archive project** and **Back from the archive** on the project's Stands & access page, and `project_upsert(archived=true|false)` (MCP, additive, coordinator sessions only, like `task_archive`). An archived project leaves the sidebar, All boards, `project_list`, the Inbox and `portfolio_digest`; its board and cards are kept, and All boards links to "Archived projects". Event `project.updated` with `changes.archived`.
- Binding at start: a `SessionStart` from the Claude desktop window (attended) naming a desktop session the board does not know creates that session and links its hooks exactly, so owner comments reach it from its first turn without `hook_session`. Nested and unattended starts, and starts that do not say, create nothing, as before.
- The `PostToolUse` of the desktop start tool binds the started session to the card its input names: exactly one open card key (`<project>-<number>`) binds it (`bound_by` `launch`), and the starter's binding ends as `handed_over`; none, several, a card any other session holds, or a started session the board already knew (one with live hooks met over 10 minutes before the launch, or one another session started) bind nothing, and `session.launched` says why (`card` or `unbound`). Only card keys are read from the input; nothing of it is stored. Migration 0052 adds `session.launched_by`.
- `task_create(bind=false)` (MCP, additive) creates a card for someone else without binding the calling session.
- File documents (design 0026): `doc_update(base_version=0, source_path=<absolute path>, body="-")` or **New document: a file on disk** on Docs & rules adds a document whose text stays in a file anywhere on the machine (UTF-8, at most 8 MB). The resolved path is refused, at the add and at every read, in the data folder (except `worktrees/`), the token and `desktop.toml`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.docker`, `/proc`, `/sys`, `/dev`, for the names `.env*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `*.kdbx`, `credentials*`, and on a network share unless `attachment_roots` names it. relaybay reads it on demand (size and time, then the SHA-256), indexes it again when it changed and records one `document.updated` event with the sections added, removed and changed; it never writes the file, and `doc_update` on it is refused with the file and the section's lines. A missing file keeps its document and last outline, marked "file missing since …". Every line a secret covers is masked in the index (a private key to its END line, a heading's title and anchor included), not refused. `doc_history` lists the changes seen in the file; export leaves file documents out.
- The section index (migration 0053): one row per section with its lines, size and heading path, rebuilt on every write and for a changed file. New MCP tools `doc_outline` (headings with level, anchor and alias, lines, characters and tokens; `depth`, `under`) and `doc_search` (hits per section with the heading path, the line and the lines next to it). `doc_get` gains `lines` and `max_chars` (2,000 to 1,000,000): a read over the cap (by default 40,000 characters, only for a document over 200,000) returns the first lines, a bounded outline of the rest and the call that reads on, and is never longer than the cap. `memory_search` names the section of a document hit.
- Explicit anchors: `<a id="name"></a>` above a heading is an alias of that section for `doc_get`, links, search and the page.
- `doc_update` gains `lines` and `expected`: lines `a-b` are replaced while they still read as `expected` (trailing blank lines do not count and are kept), otherwise a conflict with their current text.
- The document page has a table of contents that stays in view (two levels, sizes, the current section marked) and a search inside the document; a file document shows its file, hash and changes. A search hit on Docs & rules links to its section.
- `portfolio_digest` gains **Open tasks**: one line per open card not already listed (stage, note, waiting on, next step, pull requests, document).

### Changed

- A bare Claude Code id passed as `session` that the hooks know resolves to that session, with a note, instead of creating a session named by the id.
- `task_unarchive` answers `… is in the <stage> column, not in the archive.` (it said "done column" for every card); refusals of the archive say `Only a done or live task goes to the archive; …`.
- A quieter Inbox (design 0026). Each open card that needs the owner is one row with its reasons as marks: waits for you, to accept n/m, replies unread, comments not delivered (with why), rule warning; a card is never listed twice. Done cards are not in the Inbox, except for an agent reply written after the close: their rule warnings and older replies stay on the card, and moving a card to done marks its agent replies read. `require_tracker_log` only applies in projects that keep a tracker (any of their cards has or ever had a tracker reference, or has a tracker link now): elsewhere a move to done gets no work-log warning, done cards leave the done column by age and show no "not logged" badge. The work-log reminder is one line that unfolds to the cards; hook sessions that are not linked are one line linking to Sessions, ended ones are not counted. The sidebar badge and `portfolio_digest`'s "waiting for the owner" count the cards, questions, approvals, proposals and rules notices, not the grouped lines; the digest's "Waiting for the owner" section lists the same rows as the Inbox, questions and approval requests included.
- The plan reminder goes only to the bound session of a card in plan or dev, only with a line that asks for something (no "all N steps done" outside dev), and never to a session named in `coordinators`. The periodic repeat is only for cards in dev; a todo change that only moves "Now doing" sends nothing; a subagent's tool call (`agent_id` in the hook input) gets none.
- The project pulse is derived from the cards: cards moved and reports in the last 7 days, cards waiting for the owner, the newest report's first line with its card and age; shown on All boards, Docs & rules and in the digests. `project_pulse_set` writes an optional note shown with its date under it, with no stale mark; a stage move no longer adds a `Note:` about the pulse, and the MCP instructions no longer ask to keep it current. `pulse_stale_days` is accepted and ignored.
- The "Linked document sections" of `task_context` and the session start are pointers (title, lines, size, file, the `doc_get` call), no longer the section's first lines; the card's document field counts as a link.
- `replace_section` changes only the section's own lines: replacing a section with its own text leaves the document byte for byte (it used to add or drop a blank line at the section's end).
- `relaybay import` records a document file's line ending, byte order mark and outer blank lines, and `relaybay export` writes them back, so an imported file not changed on the board exports with the same SHA-256; mixed line endings import with a warning and export as LF. The first import after the upgrade records the style of items imported before it (bookkeeping only, nothing changes on the board); going back to 0.13 needs the backup taken before the upgrade, since migration 0053 adds columns.

### Fixed

- Finding a secret that only the whole text shows (for example a flag and its value on two lines) no longer rescans every prefix of the text: it is linear, so a large text no longer holds the database for minutes.

## [0.13.0] - 2026-11-19

### Changed

- The card's Open is a menu of two items. **In desktop** opens the desktop link of a desktop session, live or ended; it is disabled for a session started in a terminal or a desktop session without a known desktop id. **In terminal** resumes any ended session with `claude --resume` in a new terminal window in its start folder, a desktop session's included (`POST /sessions/<id>/open-terminal` no longer refuses a session that did not run in a terminal); it is disabled for every live session ("running in the desktop app", "running in a terminal"). A disabled item stays in the menu with its reason.

### Removed

- MCP contract 2: the tools `run_start`, `run_status`, `run_events`, `run_settings`, `schedule_add`, `schedule_list`, `schedule_cancel`, `usage_summary`, `budget_get`, `brake_set`, `brake_status`, `intake_post`, `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`, `message_send`, `messages_list`, `message_ack` and the resource `relaybay://intake/{source}` are removed; `relaybay://contract` reports version 2 and no deprecations. They did nothing since 0.12. 0.12 announced them as removable not before 2026-12-17 (at least 30 days); the maintainer shortened that notice and removes them now. A session with a cached schema that still calls one gets `Unknown tool: <name>`; reconnecting the MCP server or a new session loads the current list.

### Fixed

- The card's Open for a terminal session reads and checks the session under the same lock as the start, so a click queued behind another cannot start a second `claude --resume` after the first terminal has started.
- Open on a board reached through a published name (`external_url`, `allowed_hosts`) is still refused on the machine itself, since a tunnel there forwards a phone's request from 127.0.0.1; the refusal now says to open the board at `http://127.0.0.1:<port>`.
- A `SessionStart` after `/clear` keeps the session's start folder, as a resume and a compaction do, so Open resumes in the launch folder and not in a subfolder the session moved into.

## [0.12.0] - 2026-11-19

### Added

- Areas: what part of a project a card belongs to ("web shop", "mobile app", "api"), one per card or none, from a list per project that grows from use (design 0024). Names match without regard to case and outer spaces; a new name is added to the project's list (at most 24 areas, 32 characters a name) with a colour from a palette of eight by its place, readable in light and dark themes. The card shows a coloured chip after its key, as do the task dialog's head, the task page, Inbox and review queue rows and the quick switcher (which also searches areas). The task form has an Area field with the project's areas as suggestions, and the board an "Any area" filter next to the stage filter, kept in saved filters. The Stands & access page lists the areas with their card counts: rename, recolour, reorder, delete (cleared from its cards, each card's history keeps it) and add; renaming one into another's name merges the two. Events `project.updated` (`changes.areas`) and `task.updated` (`changes.area`).
- MCP (additive, contract v1): `area` in `task_create`, `task_update`, `task_patch` and `relaybay://schema/task`; `task_list(area=...)`; the area in `task_get`, `task_context`, `task_list` and the digests; `project_snapshot` lists the project's areas with how many tasks carry each. A name an agent sends that is new is added, and the answer says so and lists the project's areas.
- The backlog's free-text area field suggests the project's areas first, and "To plan" gives the new card the item's area (added to the project's list when new; a text over 32 characters is not carried). Backlog rows are not migrated.
- Markdown export and import carry a task's area by name in an `area` column of `TASKS.md`, written only when a task has an area.
- Migration 0048: `project_area`, `task.area_id` and `saved_filter.area`, additive only.
- The session card (design 0025): the task dialog and the task page start with a large state line, derived and never set by hand: Working (with the session's last activity), Idle, Waits for you (an open question or approval request, a review in review or test, `waiting_on` naming the owner, or a session asking for its user, each reason listed), Ended or No session. The same word shows on the board card next to the session's dot.
- One Open in the state line follows where the session runs: a desktop session opens in the desktop app; an ended terminal session opens a new terminal window on this machine running `claude --resume <id>` in its folder (`POST /sessions/<id>/open-terminal`, board only, from the machine itself with a loopback `Host`, once until the session starts or 60 s pass, in the session's start folder, without Claude Code's environment variables; Windows Terminal or a console window, Terminal on macOS through `osascript`, the first of `x-terminal-emulator`, `gnome-terminal`, `konsole`, `xterm` elsewhere; event `session.terminal_opened`, shown on the Audit page with "Changes"); a live terminal session gets the button disabled with "running in a terminal"; an unknown surface gets only the reason.
- Copy the brief: a card without a live session gives the text a new session needs (key, title, the line to bind itself with `task_bind_session`, the task's digest with next step, description, done so far, plan, decisions, the attachments by path, the report line), to the clipboard or selected in a fold. relaybay starts nothing.
- Where a session runs: the command hook sends `CLAUDE_CODE_ENTRYPOINT` with `SessionStart` (`relaybay_entrypoint`), and the session is recorded as desktop or terminal; when the hook cannot tell (an older hook client, an http hook, another entrypoint) the session start asks the session once to say it with `task_patch(task, fields={"session_surface": ...}, hook_session=...)`. MCP (additive, contract v1): the field `session_surface` in `task_patch` and `relaybay://schema/task`, stored on the calling session (event `session.surface_set`).
- "Not delivered yet" on an owner comment names why it waits: no session bound, the session ended, the session has not linked its hooks yet, or the session is idle and the wake-up hook is off. The Inbox has a group "Comments not delivered" for comments waiting longer than an hour.
- A terminal session resumed with `claude --resume` (a `SessionStart` with `source: resume` of the same Claude Code session) is bound again to the tasks its own end unbound; a task unbound by hand or bound to another session since stays as it is.
- Docs: the how-to "Work a card with a session" (Copy the brief, a desktop or terminal session that binds itself, the state line, answers and comments, review, Open).
- Migration 0050: `session.surface`, `session.entrypoint`, `session.start_cwd` (the folder a resume starts in) and `task_session.unbound_reason`, additive only.

### Changed

- A `Notification` hook with `notification_type: idle_prompt` (Claude Code sends it about a minute after a turn ended) leaves the session idle; only one that asks for its user (`permission_prompt`, `elicitation_dialog`, or an unknown or missing type) makes it waiting.
- `relaybay import` now reads an `area` column of `TASKS.md` (also in a hand-written registry): a text over 32 characters, or a new name past the project's 24 areas, leaves the task without its area and adds a warning.
- On pages with the sidebar, the top bar's product name is a small home icon linking to the start page (labelled "Home"); the sign-in page keeps the word.
- Migration 0049 (`0049_remove_runs.sql`), data only (no table is dropped): it lists the worktrees runs created and did not remove for the Inbox (`app_setting` `removed_runs_worktrees`); cancels the waiting turns of runs and marks the running ones lost; ends every open run (status reason "runs inside the program were removed in 0.12", ended by `relaybay`); withdraws the open questions that have a run (the coordinator's questions without a card among them) and relaybay's own open questions about a kept worktree, with that reason; expires the open and the approved, unused approvals that have a run; stops the open schedule runs ("schedules were removed in 0.12") and disables every schedule not deleted or done; clears every task's "answer received" mark. Questions and approvals of desktop sessions stay open. The tables of runs, the coordinator run, profiles, schedules, usage, budgets, the brake, intake and session messages stay in the file, unread; reports that turns wrote stay on their cards.
- The Inbox has a group "Worktrees left by runs": the worktrees runs created and did not remove, each with the hint to remove it with `git worktree remove <path>` in its repository; its Seen (`POST /inbox/worktrees/seen`) deletes the list, not the folders, and writes the event `inbox.worktrees_seen`. relaybay touches nothing on disk.
- `config.toml` keys `max_running_turns`, `min_free_memory_mb`, `claude_path`, `usage_enabled` and `usage_interval_seconds` are accepted and ignored; `relaybay doctor` names them once as ignored. Their environment variables are gone from the configuration reference.
- Questions and approvals are for desktop and terminal sessions only: the answer or the decision reaches the session as an owner comment through the hooks; approvals still check a desktop session only when the policy turns on `approvals.desktop` (off by default).
- The MCP instructions have twelve points: those about the brake, about an external message (intake) and about session messages are gone.
- The approval gate never answers `allow` (it did only for runs): a covered call of a session gets no decision and Claude Code's own checks go on; the approval is reserved for that call, used at its `PostToolUse`, or counted used at the session's `Stop` or `SessionEnd` when no `PostToolUse` came. When the check itself fails the call goes on, as with a guard error. Approvals are no longer released.
- `relaybay doctor` has an ok item `ignored settings` naming the ignored keys found in `config.toml`, and lost `claude sign-in`, `pseudo-terminal` and `token budgets`. The environment variables of the ignored keys are no longer read.
- `max_concurrent_sessions` is checked by a loop of its own every 60 s (it ran after the usage pass); the `concurrency.over` event and the Inbox line are unchanged.
- A `desktop.toml` whose `notify_kinds` names a removed kind (`reminder`, `brake`, `budget`, `answer`) loads without it.
- `relaybay setup --mode http` no longer writes the `X-Relaybay-Run` header; an http install made before keeps working and the server ignores the header. Sessions no longer record their transcript path.
- The removed pages answer 404: `/coordinator`, `/schedules`, `/settings/agents`, `/settings/profiles`, `/settings/intake`, `/usage`, `/messages` and the run and terminal routes. Settings has the tabs Integrations, Policy, Rules and Notifications.
- CI runs the desktop builds on main and on a pull request that changes what goes into them; other pull requests skip them (not a required check).
- The task dialog and the task page have one order: the state line, the Questions and Approvals folds, the last report as the main content, the comment thread, then the folds Session and its history, Task details (the fields), Description, Done so far and the rest; the comment box comes last and outside every fragment that live events refetch, so a live event never clears a half-typed comment. The Last report and the Comments are no longer folds.

### Deprecated

- MCP (contract v1 unchanged): `run_start`, `run_status`, `run_events`, `run_settings`, `schedule_add`, `schedule_list`, `schedule_cancel`, `usage_summary`, `budget_get`, `brake_set`, `brake_status`, `intake_post`, `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`, `message_send`, `messages_list`, `message_ack` and the resource `relaybay://intake/{source}` stay registered with unchanged parameters and annotations but do nothing: each answers `Nothing done: this tool does nothing since 0.12.` and a notice of its feature, which also ends its description (runs and schedules: `Deprecated: runs and schedules inside the program were removed in 0.12; a card is worked by a desktop session bound to it.`; the brake: `Deprecated: the brake was removed in 0.12; stop a session in the Claude desktop app.`; usage and budgets: `Deprecated: token budgets and the usage reader were removed in 0.12; the Claude desktop app shows the plan limits.`; intake: `Deprecated: intake was removed in 0.12; the coordinator session reads the chats and updates the cards itself.`; messages: `Deprecated: messages between sessions were removed in 0.12; sessions message each other through the Claude desktop app.`). The contract lists all of them under Deprecations, removable not before 2026-12-17 in a later minor release, which raises the contract to version 2.

### Removed

relaybay is the board next to the Claude desktop app, which shows and drives an agent's session better than a card can, so everything that only a run inside the program needed, and what the desktop app already does, is removed (design 0025):

- Runs on cards: the runner, turns with `--resume`, worktrees per run, the Agent block in the task dialog and its routes, run events and their retention, handing a run to the desktop app, slots, the free-memory check and Start anyway.
- The Terminal tab (the pseudo-terminal, the vendored xterm.js, its WebSocket route) and the `pywinpty` and `wsproto` dependencies.
- The coordinator run (its folder and starter `CLAUDE.md`, wakes, compaction, its budget), the Coordinator page and its sidebar mark. The coordinator role stays: sessions named in `coordinators` still use `rules_update`, `plan_accept`, `task_archive`, `brake_set` and the rest.
- Permission profiles and Settings → Profiles, the project run defaults (the Agent defaults block on the Stands & access page), and Settings → Agents.
- Schedules of every kind (coordinator turns, read-only checks and reminders), the Schedules page and its sidebar item, and the `reminder` tray notification.
- The run roads of questions (an answer delivered as a run's next turn) and of approvals (requests from a run's permission denial, a decision delivered as a turn, its release), the `RELAYBAY_RUN_ID` link of a run's hooks and the `X-Relaybay-Run` header of http hooks.
- The brake (Stop all, the banner, `relaybay brake on` refusals through the hooks) and token budgets (the Budgets block, the task's budget row, budget lines through the hooks, the `brake` and `budget` tray notifications): built for agents on cards, and the desktop app shows the plan limits. The per-session "No live now" switch and the guard stay.
- The Usage page and its sidebar item, the background reader of Claude Code transcripts and the transcript paths kept only for it.
- Intake from chats and e-mail: the Inbox groups Intake and Answers, the "answer received" mark, source trust and autonomy (Settings → Intake), Forward to the agent, the task's Intake block, intake wake-ups and notifications, `examples/intake/` and its how-to. Without a coordinator inside the program nothing can tie a customer's message to a task; the coordinator session reads the chats and updates the cards. The backlog stays.
- Messages between sessions: the Messages page and its sidebar item, the task's Messages block, delivery through hooks, their limits, notifications and Inbox lines; sessions in the desktop app message each other through the desktop app.
- Docs: `docs/PLAN.md`, the pointer page to the former plan; its successors are the roadmap, the specification and the architecture page.

## [0.11.0] - 2026-11-16

### Added

- Permission profiles as data, edited on the board (Settings → Profiles, board only): a profile holds the permission mode, the allowed and the taken-away tools (`--allowedTools` and `--disallowedTools`, one rule per line), the board rights a run under it will have and the run kinds it is for. `agent`, `coordinator` and `read-only` ship; the owner may add more, edit any, reset a shipped one and delete an added one no project or open run uses. A project's agent defaults name a profile instead of their own mode and tools, and the Start form offers the profiles for cards. Every turn reads its run's profile when it starts, so an edit takes effect on the next turn of every run under it; each turn records the profile's name and version, shown on the card and in `run_status`. A mode picked on the Start form or changed between turns stays the run's own; the owner's "the profile's" choice makes it follow again. Every turn takes away Claude Code's own scheduling tools (`CronCreate`, `CronDelete`, `ScheduleWakeup`, `RemoteTrigger`), whatever the profile says, and no profile is `bypassPermissions`. Events `profile.created`, `profile.updated`, `profile.reset`, `profile.deleted` with the values before and after (on the Audit page). The coordinator run enforces the rights (below). Migrations 0043 and 0044: a project whose mode and allowed tools differ from `agent`'s keeps them as a profile `agent-<project>`, so no run changes its rights by the upgrade.
- The coordinator as one permanent run without a card, on a new Coordinator page (`/coordinator`, in the sidebar; owner only, cookie and CSRF): Start makes it ready idle, with no turn and no tokens; the owner's box sends a turn framed as the owner's word ("The owner says on the board: ..."); Stop, New session (a fresh Claude Code session; its `CLAUDE.md` and relaybay's memory carry over), End, Terminal and Open in the desktop app work as on a card's run; the page lists its turns with their result text and its open questions. It runs in a folder of its own that is never inside a git repository (default `<home>/coordinator`, else the per-user data folder; Settings → Agents sets another one, its profile, model and effort; Start refuses a folder inside a repository), where relaybay writes a starter `CLAUDE.md` once and never overwrites it. Its turns use its profile (`coordinator`: `dontAsk` with an allow list) plus one rule that lets Edit and Write work inside its folder only. A call carrying its Claude session id as `hook_session` is the coordinator for every check that asks the `coordinators` config, under its profile's rights: each right off is refused, and `run_start` is capped at `run_start_per_day` (10) a local day; a desktop coordinator of the config works as before. `question_ask` takes no `task` for the coordinator, whose answer comes as its next turn; `run_status` takes `coordinator`. It takes no slot of `max_running_turns` (the memory check applies), a project's brake does not hold it (`all` does), and no card's close or project budget touches it. Events `coordinator.started`, `coordinator.new_session`, `coordinator.stopped`, `coordinator.ended`, `settings.coordinator` (on the Audit page). Migration 0045 rebuilds `run` (with `kind`; a card and a project only for runs on cards) and `question` (a card or a run), keeping every row.
- The coordinator is woken by events on the board: a report on a card, a run on a card that ended, stopped or was lost, a question past its deadline (new event `question.overdue`, once per question; a blocking one after an hour open), a plan draft waiting for acceptance, an approval the owner decided, new or tied intake, a message to the coordinator and a budget threshold. The events since the last wake become one turn sent by relaybay, framed as data and not instructions, one line per event with its keys, at most 50 lines and then where the rest is (`events_since`). Its own actions and its own budget wake nothing, events during a turn go into the next wake, and a minimum gap (Settings → Agents, default 120 s) turns a burst into one turn. While it is stopped, ended, waiting for your answer, queued, with you in the Terminal tab or the desktop app, braked or over a `stop` budget, the events wait and the Coordinator page shows how many and why. A wake whose turn never ran (cancelled, stopped or lost before its result) gives its events to the next wake. Each wake is a `coordinator_wake` row; the page folds a wake's lines under "woke by N events". Events `coordinator.woken`, `coordinator.compacting`, `question.overdue`.
- relaybay compacts the coordinator's session itself: after a turn whose context (input, cache read and cache write of its last assistant message, not the sum of its API calls) passes Compact at (Settings → Agents, default 120,000 tokens) the next turn is `/compact`, before any wake, in the same Claude Code session, never right after Claude Code compacted the turn itself and never again while the last `/compact` did not bring the context under it; the page shows "compacted: X -> Y tokens".
- The coordinator's own token budget, of its session per UTC day: Start creates it from Settings → Agents (daily tokens, default 3,000,000; at the limit `warn` by default or `stop`), New session takes it over, and the Coordinator page header shows today's tokens against it with the warning at 80% and 100%. A `stop` budget brakes its session and holds its wakes. Budgets get the scope `session` (set only for the coordinator).
- Migration 0046: `coordinator_wake`, `question.overdue_at`, `run_turn.compact_pre_tokens`, `compact_post_tokens` and `context_tokens`; `token_budget` rebuilt for the `session` scope, keeping every budget, mark and brake.
- The Coordinator page complete: a header with a line for its state (no coordinator yet with what Start does and where its folder will be, ended, with you in the Terminal tab or the desktop app, stopped, over a `stop` budget), then **What waits for you** (its open questions and the open approvals of the runs it started, answered and decided on the page), **What came in and what it decided** (each wake's event lines beside its result text, the owner's turns and relaybay's `/compact` marked, newest first, 20 a page with Older turns), **Runs it started today** (card, title, status, started, tokens) and **Its schedules** with a link to Schedules. The page refreshes itself on the coordinator's, its runs', question, approval, schedule and budget events, and the sidebar marks Coordinator while its questions wait or its budget warned today. The board now listens to `coordinator.woken`, `coordinator.compacting` and `question.overdue`, which the Activity filter lists too.
- Schedules: a clock in the program, on a new Schedules page (`/schedules`, in the sidebar; owner only, cookie and CSRF) with each schedule's kind, when, target, next time, last outcome and who made it, its runs, a form to add one, and Edit, Enable / Disable, Run now, Stop its check and Delete. Three kinds: a **coordinator turn** (a `schedule 3: <text>` line in the coordinator's next wake, skipped while no coordinator run is open), a **read-only check** and a **reminder** (a tray notification of the new kind `reminder` and an Inbox line under Schedules until marked seen, no model). When: once at a date and time, daily at a time (optionally weekdays only), or every N minutes (at least 5) at most 24 times. Times are the machine's local time and a daily time follows daylight saving changes. A time missed by less than an hour (the program closed, the machine asleep) runs once when it is back, an older one is logged as missed; one run of a schedule at a time, a due time meanwhile is skipped. Events `schedule.created`, `schedule.updated`, `schedule.deleted`, `schedule.fired`, `schedule.missed`, `schedule.skipped`, `schedule.ended` (on the Audit page) and `schedule.notice_seen`.
- A read-only check is a run of the new kind `check`: one `claude -p` turn without `--resume`, with `--no-session-persistence` and `--max-turns 8`, under the `read-only` profile (or another profile for checks) with `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `PowerShell` and `mcp__relaybay` taken away, in the worktree of the card's open run or the project's folder. It takes a slot and waits for a slot, memory or a brake like a card's turn, and its tokens count in the project's and the card's budgets. Its answer becomes a report on the card, first line `inspected · S · <gist>` written by relaybay (without a card: an Inbox line and the Coordinator page); `error_max_turns` reads "check stopped at its step limit". A check every N minutes stops when its answer starts with `done`. The run ends after its one turn.
- MCP tools for the coordinator run, under its profile's `schedule_add` right (anyone else is refused): `schedule_add` (a one-off check or reminder at a local time or in N minutes, or a check every N minutes bounded by `max_times`; never a coordinator turn, a daily schedule or a repeating reminder: those are the owner's on the board), `schedule_list` (its own and the owner's) and `schedule_cancel` (its own only); at most 10 of its schedules may still run and every run of its checks counts in its `run_start_per_day`. The Coordinator page lists its schedules with the next time.
- relaybay's PreToolUse hook denies a check's or the coordinator's call that no allow rule of its profile matches (the coordinator's file writes only inside its folder; a Bash command one command only, without options such as git's `--output` or `-c`), whatever the user's settings pre-approve under `dontAsk`; it fails open like every hook.
- Migration 0047: `schedule` and `schedule_run`.

## [0.10.0] - 2026-11-13

### Added

- The Terminal tab on a run, off by default (Settings → Agents → Terminal tab, board only): a Terminal button on a run with a known session opens `claude --resume <id>` in the run's folder in a terminal on the board, a pseudo-terminal on this server (ConPTY through pywinpty on Windows; the standard library's `pty` on macOS and Linux, not probed there) shown by the vendored xterm.js 5.5 over a WebSocket. While it is open the run is with you: no turn starts and texts sent wait, because two writers on one session fork its transcript; closing the tab, `/exit`, Back to the program or End kills the process tree first, then hands the run back and the first waiting text starts. The socket is loopback only and needs the board's cookie, its own Origin and a one-time token from a board-only POST; Bearer clients are refused. Ctrl+V pastes, and a hint names the trust question Claude Code asks in a new folder. Events `run.terminal_opened`, `run.terminal_closed`, `settings.terminal_enabled` (on the Audit page); migration 0042. When the hand-back is refused (the sign-in expired) the page and the card say so and Back to the program tries again; the button stays hidden on a server not bound to loopback; `relaybay doctor` has a `pseudo-terminal` item; the terminal's one-time token is redacted from the log. New dependencies: `pywinpty` (Windows only) and `wsproto` (uvicorn's WebSocket support).

### Fixed

- A run started by a server that itself runs inside a Claude Code session no longer inherits that session's markers (`CLAUDE_CODE_CHILD_SESSION` and the other host `CLAUDE_CODE_*` variables): with them the run's `claude` saved no transcript, so `--resume` had nothing to go on. A provider, shell or output setting the user exports (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_GIT_BASH_PATH`, ...) still reaches the run.

## [0.9.0] - 2026-11-13

### Fixed

- An approved push could be lost to a slow approval check: the command hook gave the guard one 0.3 s try while the check reads the branch with git, so on a slow machine Claude Code refused the push and the approval was already used. The hook now waits up to 5 s for a call that may need an approval (a push, a PR merge, a stand change; its `PreToolUse` and `PostToolUse`), the hook entries' timeout is 10 s for command and http hooks (an older setup's 3 s or 2 s shows as `hook timeout` in `relaybay check` and doctor, and the desktop program points to Settings → Reconnect; run `relaybay setup` again to update it), the forwarder's check of the body reads only what comes before the tool's output and stays fast on a large one, and the approval check keeps git within 1.5 s per read and 3 s in all, reuses a branch read while the `HEAD` file is unchanged, and denies "could not tell the branch, run it again" when git is too slow. An approval is now reserved for the call it allows (by its `tool_use_id`) and spent only when that call may have run: its `PostToolUse`, or the end of its turn (or a lost turn) without a refusal of it. A call in the turn's `permission_denials` (a classifier's refusal included) releases every approval it held, with no new request and a report line saying it stays for one more try; the third release expires it ("released 3 times"). Event `approval.released`; migration 0041.
- Runs on macOS: stopping a turn whose process group holds only exited processes no longer fails with "Operation not permitted" (macOS answers EPERM there).

### Added

- Runs on a card: the card's Agent block starts your own unmodified `claude` on this machine, under your own sign-in, in one of the project's repositories. The first turn gets a brief built from the card; Continue resumes the same Claude Code session (`--resume`), a text sent during a turn waits for it, Stop kills the turn's whole process tree (a job object on Windows, a process group on macOS and Linux), End closes the run. The card shows the status, the Claude session id and each turn's tokens and denials. When relaybay's hooks or MCP server are missing from the Claude Code user settings, the run passes them itself (`--settings` with exactly the command hook `relaybay setup` writes, so it never fires twice, and `--mcp-config`; both files mode 0600 in the data folder, never `--strict-mcp-config`). A second open run on the card or in the same folder is refused.
- The login check: before a run, `claude auth status` is asked for the sign-in method only (never the e-mail or organization). A subscription starts; signed out shows `claude`, then `/login`; an API key or another method starts only when Settings → Agents allows it. `relaybay doctor` has a `claude sign-in` item.
- Settings → Agents (the `claude` in use, the sign-in, where hooks and MCP come from, the sign-in switch) and per-project Agent defaults on Stands & access (model, effort, permission mode, allowed tools, `--chrome`).
- `claude_path` (`RELAYBAY_CLAUDE_PATH`): the `claude` a run starts; by default `claude` on PATH, then the usual install folders.
- A run's hooks link to the run's own session row by `RELAYBAY_RUN_ID`; a `claude` started inside a turn gets a session of its own, never a guess onto a desktop session.
- Runs over several repositories: the Start form chooses the working folder (the project folder from the Agent defaults, or a repository) and, per repository, a new git worktree (under `<data folder>/worktrees/<card>-r<run>/`, on branch `relaybay/<card>`) or the repository itself. Every folder other than the working one is passed with `--add-dir`, with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` so each repository's `CLAUDE.md` loads. A new worktree gets the repository's untracked `CLAUDE.md`, `CLAUDE.local.md`, `.claude/` and `.mcp.json` (or the gitignored files its `.worktreeinclude` matches, as Claude Code does) copied in, never added to git; the form says in one line what. Every check comes before the first `git worktree add`; when a Start still fails, or is cancelled, the worktrees it made are removed again, and a taken branch or folder name moves on to a numbered one. One open run per folder.
- MCP `run_start` (coordinator sessions only, not under a brake themselves: cards in plan or dev, no brake on them, always new worktrees, the project's permission profile; anyone else is told a side task goes to `backlog_add`) and `run_status` (open to every session).
- `run_worktree` table (migration 0033).
- Open in the desktop app: an idle run's Agent block opens its Claude Code session in the Claude desktop app (`claude --desktop --resume <id>` in a console of its own on Windows, its refusal shown when it fails; on macOS and Linux the `claude --resume <id>` command to run in a terminal). Only the run's working folder is a working folder there. While the run is with you there, relaybay starts no turn and texts sent wait; **Back to the program** starts the next one. The run then links to `claude://claude.ai/epitaxy/local_<id>`. Events `run.opened_in_desktop` and `run.returned`; migration 0034 adds the columns.
- A run's permission mode, model and effort change between turns, from the next turn on: any mode by the owner on the card, only a lower or equal one (never `bypassPermissions`) by a coordinator with the new MCP tool `run_settings`. The order is documented in the SPEC; every change is a `run.settings_changed` event, also on the Audit page.
- A run's turns are kept: every stream-json line is stored (`run_event`, migration 0035), each string masked with the journal's secret masking before the line is compressed (also a quoted secret name before its value, and the whole string under a field such as `password`, `api_key` or `Authorization`), never stored or logged unmasked; subagent lines keep their `parent_tool_use_id`. A line over 4 MiB keeps only its masked head with a marker. Lines are stored in batches (200 lines or 0.5 s), so storing never slows the turn.
- The full report of each turn lands on the card: the turn's final text as a report (the board, digests and MCP see it like any `report_add`), its refusals (`permission_denials`, input masked and cut) under it. The Agent block lists each turn's report and opens, folded until asked, the report and the turn's lines: tool calls with their results, subagent work nested under its call.
- `verified: <where>` marks on report claims (`report_check`, migration 0036), from `report_add` and from runs: shown under the report on the card; an M or L report without any gets a `Warning (rules)` line (never a refusal, never for S).
- MCP `run_events` (open to every session, read-only): a turn's lines as short masked items, at most 200 per call and 20,000 characters. `run_status` shows the first line of the last turn's report and the latest turn's lines by type.
- Questions to the owner as objects (`question` table, migration 0037): MCP `question_ask` puts a question on a card with options, a required default and `blocking`, or of kind `look` ("look at this": always blocking, the URL of a screen the agent brought up and the test account's name, answered fine or remarks). At most 5 open per card; a question or field that looks like a password or token is refused by the policy's secret check. `question_list` and `question_withdraw` (the asking session or a coordinator); no MCP tool answers. The Inbox lists the open questions of all cards (card, run, who asks, "blocks the run" or "goes on with the default at", age, option buttons and a text field, the look URL opening in your browser); the card shows its own; you answer with `POST /questions/{id}/answer` (board only). A run waits (`waiting`, the card waiting on you) when its turn ends with a blocking question open, and your answer starts the next turn at once with `Owner's answer to question N ("..."): <answer>` verbatim; an answer during a turn waits as the next turn and the hooks tell the turn once; a run held in the desktop app gets it after Back to the program; a desktop session gets it as an owner comment through its hooks and the wake-up poll. A non-blocking question closes with its default at `default_after` (default one hour; a server check every 15 seconds and on reads, so a restart only delays it); a later answer reaches the agent as a change of course. Events `question.asked`, `question.answered`, `question.defaulted`, `question.delivered`, `question.withdrawn` and `run.waiting`, on the Activity and Audit pages. A new tray notification kind `question` (on by default) for each new blocking question; it absorbs the "waiting on you" notice of the same card. An answer whose turn never started (Stop, End, a lost turn, a restart) goes with the run's next turn, or as an owner comment when the run ends; a coordinator's brief may not carry a line that reads like the owner's answer. A half-typed answer is not wiped by live refreshes.
- Archive a done card by hand: **Archive** in the card menu and the task view takes it off the done column at once (also with `require_tracker_log`); "Show archived" and search still find it, and **Back from the archive** returns it for another full done window. Any stage change clears the mark. MCP `task_archive` / `task_unarchive` for coordinator sessions only; events `task.archived` / `task.unarchived` on Activity and the Audit page. Migration 0038 (`task.archived_at`, `archived_by`, `unarchived_at`).
- Approvals as objects (`approval` table, migration 0039): a run's push, PR merge, commit or change on a test or live stand that the policy names needs the owner's Approve on the board. A new policy key `approvals` (`push`, `merge`, `test` true and `commit` false by default, `live` always true: a layer that drops it is refused; `desktop` false: desktop sessions are checked only when a project sets it; `disable` switches off a built-in pattern, `ignore` lists command segments that are never an action, neither ever narrowing live). The guard's `PreToolUse` check reads `git push` (with its branch: refspecs, `HEAD`, the current branch; force pushes and deletes as their own modes; `--tags` and the like as targets of their own; `--dry-run` passes), `gh pr merge` (the PR) and the stand changes of the access map, git actions and stand changes of one call each needing their approval; a push a run makes in a form the guard cannot read (`bash -c`, `sudo -u`, a subshell, after `cd`) is denied. A call without an unused approved approval for its card and target is denied with "waiting for the owner's approval on the board" and opens a request; one approval allows exactly one matching call run alone from the run's folder within `approvals.valid_minutes` (default 120; then it expires), answered `allow` for a run not with the owner in every mode but `bypassPermissions`; with other commands in the same call it is denied and kept. Commands that only mention a push or a merge (`grep`, `echo`, `gh pr create`, a here-document commit message) are not read as one. If the check itself fails, a run's push, merge or stand change is denied ("approval check unavailable"), the run found by its session. A card that goes to done withdraws its open questions and expires its open requests and unused approvals (`approval.expired`). The runner opens a request from each `permission_denials` entry of a turn that needs one and has none open; a refusal of Claude Code's own auto mode no rule covers stays a refusal. MCP `approval_request` (summary, rollback required for a deploy, the secret check) and `approval_list`; no tool decides. The Inbox's Approvals group and the card's Approvals fold with Approve / Refuse and an optional line (`POST /approvals/{id}/decide`, board only); a used approval shows when and by which call. The decision reaches the agent as the run's next turn (`The owner approved push of <branch> …`, `The owner refused …: <line>`) or as an owner comment for a desktop session, through the questions' delivery; a run waits on an open request at its turn's result; a coordinator's brief may not carry a forged decision line. Events `approval.requested`, `approval.decided`, `approval.used` on Activity and Audit; a tray notification kind `approval` (on by default).

- Run limits: before each turn a free slot (`max_running_turns`, default 2, over all runs) and enough free memory (`min_free_memory_mb`, default 1500; read by platform code, no new dependency) are checked, and no brake may hold the run. Otherwise the turn waits `queued` with its reason ("waiting for a slot: 2 of 2 running", "waiting for memory: 1.2 GB free, 1.5 GB needed", "braked (…)") on the card, in the Inbox's new Queued turns group and in `run_status`; it starts by itself, first queued first, when a slot frees, memory recovers or the brake is released (a re-check every 15 s). **Start anyway** (board only) goes past the slot limit and the memory check, never past a brake. Both limits are in `config.toml` and editable in Settings → Agents.
- The brake on runs: a refused tool call of a run's turn under a brake also kills the turn's process tree (builds and servers with it); the run's next turn waits until the brake is released. A stop budget acts the same.
- A card's close: when a card goes to done its run ends, each worktree the run created is removed (`git worktree remove`, after a check for changes, untracked files git does not ignore, Claude files that changed or are new (`CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `.mcp.json`, `.worktreeinclude` files), and commits on no remote or other branch; other ignored files such as `node_modules/` or `dist/` go with the worktree; the copies come back if git refuses); one with work left is kept, listed on the card, and relaybay asks you on the card whether to remove it (`remove` uses `--force`, `keep` is the default). A run that saw a secret (the masking changed one of its lines) or touched a live stand has its stored lines and what Claude Code keeps of its own session deleted (the transcript, the session's folder, `file-history/<id>/`, `session-env/<id>/`; a failed delete is tried again). A project keeps its runs' lines 30 days by default, or not at all (`off`), set in the Agent defaults; older lines are deleted hourly. New events on the Audit page: queueing, Start anyway, worktree removals, the marks, deletes of lines and transcripts. Migration 0040 `run_lifecycle`.

### Changed

- A run's queued turn now survives a restart of the server (only the texts behind a lost turn are cancelled).
- Secret masking (the live-actions journal and run lines): a PGP private key block is masked, and the secret-name pattern is bounded, so masking a long run of word characters is linear instead of taking seconds.
- The http hook that `relaybay setup --mode http` writes also sends `X-Relaybay-Run: ${RELAYBAY_RUN_ID}`; apply setup again before starting runs with http hooks.

## [0.8.6] - 2026-11-12

### Fixed

- Stopping the server ends the wake-up hook's long polls at once (they answer 204), instead of holding the shutdown until the graceful timeout cancelled them with a 500 and an error in the log.
- The owner's plan acceptance on the board now reaches the bound session when it is idle: the
  wake-up hook wakes it with the `Owner accepted the plan of …` line, as it does for an owner
  comment. Before, only a tool or prompt hook told it, so a session that had asked for the
  acceptance and ended its turn heard nothing.
- Board: the rule warnings of a card move have a close button (×, keyboard reachable; the focus
  then goes to the moved card's menu button) and stay
  until the owner closes them or moves again; the "needs an accepted plan" line goes away once that
  task's plan is accepted.

### Added

- Coordinator sessions (`coordinators` in the config) are told each plan acceptance on the board,
  in any project, once, through the same hooks: `Owner accepted the plan of <key> (<n> steps) on the
  board, bound to <session>.` Acceptance lines take at most 2000 characters of one hook answer;
  the rest, and an owner comment that no longer fits beside them, come with the next hook.

## [0.8.5] - 2026-11-10

### Fixed

- Desktop on Linux under Xorg: a tray without notifications no longer fails a menu action or the watchdog's notice; the message stays in the log.

### Added

- Desktop: the start journal and the log say who asked the program to quit (the tray menu, another run's `--quit`, an update, the window loop's end, `--exit-after`).

## [0.8.4] - 2026-11-10

### Fixed

- Desktop on macOS: the program's process ends after Quit and before an update; it could stay alive after a clean quit.

### Added

- Card order on the board: drag a card within its column to set its place, or into another column
  to change its stage (the same rules and warnings as the edit form; a refusal puts the card back
  and says why). Mouse, pen and touch (hold, then move), a drop line, auto-scroll near the edges;
  the card's ⋯ menu has **Move up**, **Move down** and **Move to <stage>** for the keyboard and
  phones. New tasks and tasks moved to another stage go to the top of their column; other edits no
  longer move a card. On the board this order is the priority: a task's `priority` no longer sorts
  the cards (it still orders `task_list`). A reorder writes one `task.reordered` event, refreshes other open boards and
  is left out of digests and notices. Owner only: cookie + CSRF, no MCP tool.
- `TASKS.md` export writes a `rank` column, and an import with a `rank` (or `order`) column sets
  the order; a registry without it leaves the order to the board.
- Migration `0028_task_rank`: `task.rank`, filled from the order the board showed, so nothing moves.
- Projects have any number of repositories (name, path as written, kind `app`, `web`, `api`, `lib`
  or `other`): a Repositories block on the project's Stands & access page (add, edit, remove,
  reorder) and `project_upsert(repos=[…])`. `repo_path` stays and is always the first repository's
  path; the upgrade (migration 0029) turns each project's `repo_path` into its first repository.
- Tasks link the repositories they touch and their pull requests: `task_update` / `task_patch` take
  `repos` (`-name` removes one) and `prs` (`-url` removes one; a link whose path names a repository
  is tied to it). A session bound to a task links the repository it works in by itself
  (`how: auto`, left out of the digests' changes).
- A Code row on the card and in the task dialog: each linked repository with its kind, the task's
  branch, its pull requests (`#123`) and **Open in VS Code** (`vscode://file/<path>`), or a folded
  **Repos: all** line when none is linked; compact cards show `Code: 2 repos, 3 PRs`. `task_get`
  shows them under `## Code`.
- `relaybay export` writes `projects/<slug>/code.md` (repositories and the tasks' links) and
  `relaybay import` reads it back, adding what the board lacks.

## [0.8.3] - 2026-11-10

### Fixed

- A hook status change wrote one `session.status_changed` event per task the session is bound to,
  and every new tool name was a change: a session bound to 16 tasks wrote 16 identical events per
  tool call. Now one event per status change and project, with the bound tasks in `keys`; the
  activity alone is stored at most once per 60 s per session, the latest one when the 60 s end. The board, Activity and
  `events_since` read `keys`.
- The bound session was not told when the owner accepted its plan on the board; its next hook now
  says `Owner accepted the plan of <key> (<n> steps): go on with step <n>.` (the first step not
  done, or `all steps are done.`), once and only within 2 hours of the acceptance.

## [0.8.2] - 2026-11-10

### Removed

- The Map page (`/map`, `/map/graph`), its sidebar item and the vendored Cytoscape.js: without agents
  inside the program it showed little; an agents map comes back with the runner. The hooks still
  record `session.launched` and `session.message` (no text), shown in Activity.

### Fixed

- Tests wait 45 s for a test server to start on slow CI runners.

## [0.8.1] - 2026-11-09

### Added

- Map: an **Agents** view, now the default. Sessions in a box per project (Other without one), each
  with its status and current task and stage; edges for who started whom, who wrote to whom
  (`SendMessage` and `message_send`, thicker with more messages) and the owner's comments, only of
  the last 7 days. The project select comes first; with a project chosen, sessions of other projects
  linked to it stay, faded. Tapping a node lists its tasks and links; tapping an edge shows its
  count and latest times. Structure and Now are unchanged.
- Hooks: a `PostToolUse` of the desktop tool that starts a session records `session.launched`
  with the caller and the new session's desktop id only, nothing else of the tool's input or answer.
  The `relaybay hook` forwarder retries and spools it. No backfill of earlier launches.

### Fixed

- Two writes in one clock tick (about 15 ms on Windows) no longer share a timestamp, so a report right after the plan closed counts as work after it.

## [0.8.0] - 2026-11-09

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
- A versioned MCP contract (version 1): a release only adds tools, parameters, resources and
  prompts; a removal, a narrowed type or enum, a new required parameter or a read-only tool that
  starts to write is allowed only after a deprecation of at least one minor version and 30 days.
  `src/relaybay/mcp_server/contract.py` holds `CONTRACT_VERSION` and `DEPRECATIONS` (none in this
  release); a `@deprecated` tool or parameter adds a `Deprecated: …` line to its description and to
  its result. A new resource `relaybay://contract`, a **Contract** section in the MCP reference;
  server instruction 6 names the `Deprecated:` line. `tests/test_mcp_contract.py` holds the server
  to `tests/fixtures/mcp_contract_baseline.json`, which only a release pull request rewrites, with
  `scripts/gen_reference.py --rebaseline`.

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

[Unreleased]: https://github.com/Codenator81/relaybay/compare/v0.15.2...HEAD
[0.15.2]: https://github.com/Codenator81/relaybay/releases/tag/v0.15.2
[0.15.1]: https://github.com/Codenator81/relaybay/releases/tag/v0.15.1
[0.15.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.15.0
[0.14.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.14.0
[0.13.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.13.0
[0.12.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.12.0
[0.11.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.11.0
[0.10.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.10.0
[0.9.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.9.0
[0.8.6]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.6
[0.8.5]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.5
[0.8.4]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.4
[0.8.3]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.3
[0.8.2]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.2
[0.8.1]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.1
[0.8.0]: https://github.com/Codenator81/relaybay/releases/tag/v0.8.0
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

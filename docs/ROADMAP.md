# Roadmap

Versions and milestones, one line each. Status: **done**, **in progress**, **next**, **later**.
Behaviour of shipped items is in [SPEC.md](SPEC.md); each upcoming step has a design in
[design/](design/index.md). This page never describes behaviour.

Sizes are rough estimates in agent-days. The order follows the maintainer's own workflow first.

## 0.1.0 — first public release

Ships when relaybay installs with one command on Windows, macOS and Linux and the README shows the
live board.

| Item | Status | Design / spec |
|---|---|---|
| Board, data model and event log, token security, MCP server with the task tools, live SSE updates, phone layout | done | [SPEC](SPEC.md) |
| Desktop session links and binding history, aliases, done window and archive, tracker mark and history, next-step freshness, `task_patch` (since 0.17 folded into `task_update(fields=…)`), slimmer cards and task dialog | done | [SPEC §1–2](SPEC.md#spec-concepts) |
| Working rules contract (1.5a): MCP instructions, rules as data, `rules_get`, warn and strict checks | done | [0002](design/0002-working-rules.md) |
| Claude Code hooks and `relaybay setup` (1.6), Integrations page, `setup --check --json` | done | [0001](design/0001-hooks-and-setup.md) |
| Documentation restructure: SPEC, ROADMAP, architecture, designs, docs site skeleton, `llms.txt` | done | this page |
| Packaging: wheel and sdist, `uv tool install` / `uvx`, Docker image, tag-driven release with GitHub Release from CHANGELOG, PyPI by trusted publishing | done | [0003](design/0003-packaging-and-release.md) |
| `relaybay doctor [--json]`: versions, data dir, server, MCP and hook registration, rules hash | done | [0003](design/0003-packaging-and-release.md) |
| "Install relaybay with your coding agent" page (idempotent procedure, dry run first) | done | [0003](design/0003-packaging-and-release.md) |
| `relaybay demo`: neutral demo workspace (`acme-shop`, `demo-api`) for screenshots and testing | done | [0003](design/0003-packaging-and-release.md) |
| Community files: issue forms, PR template, CODE_OF_CONDUCT, SECURITY, GOVERNANCE note | done | [0003](design/0003-packaging-and-release.md) |
| README GIF of the live board | next | [0003](design/0003-packaging-and-release.md) |
| Repository settings: labels, Discussions (Ideas, Q&A, Show and tell), private vulnerability reporting | next | [0003](design/0003-packaging-and-release.md) |
| CI: smoke-start the server from the wheel, generated CLI/config/MCP reference drift check, links (`mkdocs build --strict`) | done | [0003](design/0003-packaging-and-release.md) |
| CI check that a user-visible change touches `CHANGELOG.md` | later | [0003](design/0003-packaging-and-release.md) |
| `llms-full.txt` (SPEC + reference in one file) | later | deferred from the docs restructure |

## 0.2.0 — working with sessions (released 2026-11-02)

| Item | Status | Design |
|---|---|---|
| Left sidebar navigation (replaces the header links), Inbox, Sessions, Activity | done | [0004](design/0004-navigation.md) |
| Comments from the board to agents, delivered through hooks, opt-in wake-up hook (11.4) | done | [0005](design/0005-comments-to-agents.md) |
| Unread comment counter on the card; wake-up hook verified against a live Claude Code | later | [0005](design/0005-comments-to-agents.md) |
| Project environments and access map, agent permission levels, guard, live-actions journal (11.4b) | done | [0006](design/0006-environments-and-access.md) |
| Policy as data: presets, layered `policy.json` / project / environment policy, custom guard patterns, secret rules, test secrets, Settings → Policy, `policy_get` / `policy_propose` (11.4c) | done | [0013](design/0013-policy-as-data.md) |

## 0.3.0 — acceptance and memory (released 2026-11-04)

Ships when a coordinator runs its daily work from relaybay.

| Item | Status | Design |
|---|---|---|
| Review stage, paged board, Review tab with checklist and screenshots, Review queue in Inbox, browser notification (11.5) | done | [0007](design/0007-review.md) |
| Memory and documents: decisions with scopes, memory notes and blocks, full-text search (11.6a) | done | [0008](design/0008-memory-and-context.md) |
| Digests per level with cursors and budgets, SessionStart context injection (11.6b) | done | [0008](design/0008-memory-and-context.md) |
| Markdown import and export of the memory layout, dry-run diff (11.6c) | done | [0008](design/0008-memory-and-context.md) |
| Task plan and checkpoints: ordered steps with status and checkpoint, draft / accepted plan, the agent's todo list shown under the current step, "work by plan" rule (M/L need an accepted plan before dev, S may go with the agent's todo), the current step reminded through hooks (11.7) | done | [0014](design/0014-task-plan.md) |

## 0.4.0 — insight (released 2026-11-05)

| Item | Status | Design |
|---|---|---|
| Screenshot annotations: drawing, numbered pins, flattened PNG for the agent (11.5b) | done | [0009](design/0009-screenshot-annotations.md) |
| Map (structure and "now" views) and Usage | done; the Map removed in 0.8.2, Usage in 0.12 | [0010](design/0010-map-and-usage.md) |
| Rules versioning, `rules_update` / `rules_propose`, Settings → Rules, MCP prompts (1.5b–c) | done | [0002](design/0002-working-rules.md) |
| Ctrl+K quick switcher, saved filters | done | [0004](design/0004-navigation.md) |
| Project documents in the database (SPEC, STATUS, TASK) and moving a markdown coordinator setup over | done | [0011](design/0011-docs-in-database.md) |

## 0.5.0 — intake (released 2026-11-05)

| Item | Status | Design |
|---|---|---|
| Intake from chats and e-mail: sources, matching to the waiting task, "answer received", autonomy switch per project and task, example Telegram and e-mail watchers | done; removed in 0.12 | [0015](design/0015-intake-and-autonomy.md) |
| Backlog: findings of sessions and reviews, intake proposals, "To plan" creates the task, `backlog.md` import and export | done; intake proposals removed in 0.12 | [0015](design/0015-intake-and-autonomy.md#backlog) |

## 0.6.0 — desktop program (released 2026-11-05)

| Item | Status | Design |
|---|---|---|
| PR1: tray, window, autostart, external server mode, `desktop.toml` | done | [0016](design/0016-desktop-program.md) |
| PR2: frozen builds and installers for Windows, macOS and Linux | done | [0016](design/0016-desktop-program.md#pull-requests) |
| PR2b: update check, in-app update, backup before a new version | done | [0016](design/0016-desktop-program.md#pull-requests) |
| PR3: setup and doctor for the installed program (`relaybay-hook`, Reconnect), install and migration how-tos, ADR-0002 | done | [0016](design/0016-desktop-program.md#pull-requests) |

## 0.6.1 — desktop fixes (released 2026-11-05)

| Item | Status | Design |
|---|---|---|
| The program follows the machine's light or dark theme, the window keeps its state | done | [0016](design/0016-desktop-program.md) |

## 0.6.2 — fixes from practice (released 2026-11-06)

| Item | Status | Design |
|---|---|---|
| Backlog page as cards, a start journal for the desktop program, clean stops, import identity, docs to date | done | [0016](design/0016-desktop-program.md) |

## 0.7.0 — reliability for the desktop-sessions stage (released 2026-11-06)

| Item | Status | Design |
|---|---|---|
| The plan as a living map: `plan_step_add`, the stage follows the plan, closed-plan warnings, draft hints | done | [0014](design/0014-task-plan.md) |
| Hook delivery with retry and spool, a server watchdog in the program, backlog done | done | [0017](design/0017-readiness-for-agents-inside.md) |
| Review: screenshot zoom, the step checkbox cursor; the window back after an in-app update | done | [0017](design/0017-readiness-for-agents-inside.md) |

## 0.7.1 — a calmer board (released 2026-11-06)

| Item | Status | Design |
|---|---|---|
| Collapsible blocks in the task view, older reports in an accordion, Compact cards, `waiting_on` means who | done | — |

## 0.8.0 — before the runner (released 2026-11-09)

| Item | Status | Design |
|---|---|---|
| Tray notifications: a task waiting on you, a review arrival, an agent's answer, a server problem, also with the board closed | done; the intake, brake and budget kinds removed in 0.12 | [0020](design/0020-tray-notifications.md) |
| Messages between sessions on the board; stop all and budgets; a fake `claude` and end-to-end tests; a versioned MCP contract; backlog priority and assignee; an Audit page and a per-agent live switch | done; messages, stop all and budgets removed in 0.12 | [0017](design/0017-readiness-for-agents-inside.md) |

## 0.8.1 — the Map shows agents (released 2026-11-09)

| Item | Status | Design |
|---|---|---|
| The Map's Agents view: who started whom, who writes to whom, which session is on which task, a project filter | done; the Map removed in 0.8.2 | [0010](design/0010-map-and-usage.md) |

## 0.8.2 — the Map removed for now (released 2026-11-10)

| Item | Status | Design |
|---|---|---|
| The Map page removed until agents run inside the program; tests wait longer for a test server | done | [0010](design/0010-map-and-usage.md) |

## 0.8.3 — fewer session events (released 2026-11-10)

| Item | Status | Design |
|---|---|---|
| One session status event per change; the bound session hears of a board plan acceptance | done | — |

## 0.8.4 — drag cards and open the code (released 2026-11-10)

| Item | Status | Design |
|---|---|---|
| Drag cards to order them and change their stage; project repositories and a Code row with PR links and Open in VS Code; the desktop process ends after Quit on macOS | done | — |

## 0.8.5 — the quit reason and small fixes (released 2026-11-10)

| Item | Status | Design |
|---|---|---|
| The start journal says who asked the program to quit; a tray without notifications no longer fails a notice | done | — |

## 0.8.6 — plan acceptance reaches the session (released 2026-11-12)

| Item | Status | Design |
|---|---|---|
| A plan accepted on the board wakes the idle bound session and reaches coordinators; the board warning has a close button; stopping the server ends the wake-up polls at once | done | — |

## After 0.8

| Item | Status | Design |
|---|---|---|
| Plugins on top of the versioned MCP contract: a manifest, an MCP server and, if needed, a panel on the card in a `sandbox`. A plugin declares what it may read; the contract's version keeps an update of relaybay from breaking plugins | later | — |
| Diff as the first plugin: one piece of work across all the task's repositories. On the card, an Open diff button per repository opens a ready tool (VS Code, a GitHub or Bitbucket compare page); an MCP tool gives an agent the task's diff. Uncommitted changes show only in a local tool. No line comments in the first version | not now (owner, 2026-11-10) | — |
| Browser as the second plugin: claude-in-chrome or Playwright MCP, no panel of its own; live hosts stay behind the No live now switch | not now (owner, 2026-11-10) | — |

Not planned: Electron and a browser panel of our own, because the browser and the diff are solved through MCP and plugins.

## 0.9 — the runner on a card, questions and approvals (released 2026-11-13)

| Item | Status | Design |
|---|---|---|
| The probes of 0012 (resume, permissions in `-p`, the owner's word, concurrent access, bare and subscription terms, token numbers) | done | [0012](design/0012-runner-and-embedded-terminal.md#probes-2026-11) |
| The server ends open wake-up long polls when it stops | done | [0021](design/0021-agents-inside.md#the-pain-it-removes) |
| A run on a card in one repository used in place: the login check, headless turns with `--resume`, model, effort, permission profile, waiting texts, stop with the process tree, end, hooks linked by `RELAYBAY_RUN_ID` | done; removed in 0.12 | [0021](design/0021-agents-inside.md#the-run) |
| A run over a project folder with a worktree per touched repository (`--add-dir`, the untracked Claude Code files copied in), relaybay's hooks and MCP server added per run when setup did not install them, `run_start` / `run_status` over MCP | done; removed in 0.12 | [0021](design/0021-agents-inside.md#the-run) |
| A run's session opened in the Claude desktop app and handed back; permission mode, model and effort between turns (`run_settings` over MCP lowers only) | done; removed in 0.12 | [0021](design/0021-agents-inside.md#plan-of-09-by-pull-request) |
| Full turn reports with `verified` marks: every stream-json line stored masked (`run_event`), the turn's report and refusals on the card with nested subagent events, the rules warning on an M or L report without marks, `run_events` over MCP | done; removed in 0.12 except the `verified:` marks and the rules warning | [0021](design/0021-agents-inside.md#the-run) |
| Questions (with "look at this" for visual acceptance) and approvals as objects on the card and in the Inbox | done; the run roads removed in 0.12, kept for desktop sessions | [0022](design/0022-questions-and-approvals.md) |
| Slots and free memory per machine, queueing with a reason; the brake and budgets on runs; lifecycle and retention | done; removed in 0.12 | [0021](design/0021-agents-inside.md#the-run) |

## 0.10 — the terminal (released 2026-11-13)

| Item | Status | Design |
|---|---|---|
| A Terminal tab on a run: PTY, vendored xterm.js, `claude --resume <id>`, off by default, loopback | done; removed in 0.12 | [0012](design/0012-runner-and-embedded-terminal.md#embedded-terminal) |

## 0.11 — the coordinator, schedules and profiles (released 2026-11-16)

| Item | Status | Design |
|---|---|---|
| The probes of 0023 (nested `CLAUDE.md`, profiles, Claude Code's own schedule tools, `/compact` headless, the cost of a wake, one-shot checks) | done | [0023](design/0023-coordinator-and-schedules.md#probes-2026-11-16) |
| PR 1: permission profiles as data (`agent`, `coordinator`, `read-only`), editable on the board in Settings → Profiles, read on every turn | done; removed in 0.12 | [0023](design/0023-coordinator-and-schedules.md#profiles-as-data) |
| PR 2: the coordinator as one permanent run without a card, in a folder outside any git repository, with a replaceable starter `CLAUDE.md` | done; removed in 0.12 | [0023](design/0023-coordinator-and-schedules.md#the-coordinator) |
| PR 3: wakes by events, compaction, the coordinator's budget | done; removed in 0.12 | [0023](design/0023-coordinator-and-schedules.md#wakes) |
| PR 4: schedules: coordinator turns, read-only `-p` checks, reminders | done; removed in 0.12 | [0023](design/0023-coordinator-and-schedules.md#schedules) |
| PR 5: the Coordinator page complete: what waits, what came in and what it decided, runs it started today, its schedules, live, paged, the sidebar's mark | done; removed in 0.12 | [0023](design/0023-coordinator-and-schedules.md#the-coordinator-page) |

## 0.12 — the board with the desktop (released 2026-11-19)

| Item | Status | Design |
|---|---|---|
| PR 1: areas (one per card from a project list that grows from use, a chip on the card, a board filter, MCP), a home icon for the product name in the top bar | done | [0024](design/0024-card-modes-thread-and-areas.md#areas) |
| The rest of design 0024 (card modes, the run as a thread, the Coordinator page) | superseded by [0025](design/0025-board-with-the-desktop.md) |  |
| Agents inside removed: runs, the terminal, the coordinator run, profiles, schedules (reminders included), the brake, budgets, the Usage page, intake and messages between sessions; questions and approvals stay for desktop and terminal sessions | done | [0025](design/0025-board-with-the-desktop.md) |
| The session card: the state line, one Open that follows where the session runs, Copy the brief, one order of the task view, why a comment is not delivered yet | done | [0025](design/0025-board-with-the-desktop.md) |

## 0.13 — MCP contract 2 (released 2026-11-19)

| Item | Status | Design |
|---|---|---|
| MCP contract 2: the twenty tools and the resource kept as deprecated stubs in 0.12 removed (announced for not before 2026-12-17, shortened by the maintainer's decision) | done | [0025](design/0025-board-with-the-desktop.md#mcp-contract) |
| The card's Open: a menu of In desktop and In terminal (any ended session resumes in a terminal); one Open at a time re-reads the session under the lock, the 409 on a published name points to `http://127.0.0.1:<port>`, a `/clear` keeps the start folder | done | [0025](design/0025-board-with-the-desktop.md) |

## 0.14 — project knowledge and a quieter board (released 2026-11-19)

| Item | Status | Design |
|---|---|---|
| A quieter Inbox and digest: no work-log reminders where a project keeps no tracker, ended hook sessions and done cards out, one row per card with marks; the plan reminder only for the card's session in plan or dev; the project pulse derived from the cards | done | [0026](design/0026-project-knowledge.md) |
| The backlog as a bug registry: kind, code location, triage note, duplicates, the found date, a link to a card, search and filters, an importer for a markdown registry | done | [0026](design/0026-project-knowledge.md) |
| Archive on live cards, Archive all shown with Undo, an archived project; a session started from a desktop session links and binds by itself | done | [0026](design/0026-project-knowledge.md) |
| Documents for large specs: a file in the project's repository indexed by relaybay, an outline with sizes, search by section, capped reads, the card's pointer to a section, a table of contents on the page; exact section writes and byte-identical import and export | done | [0026](design/0026-project-knowledge.md) |

Not planned: project managers and a Managers page (the owner, 2026-11-16).

## 0.15 — file documents written through relaybay, a task context that fits the task (released 2026-11-20)

| Item | Status | Design |
|---|---|---|
| `doc_update` writes a file document in place, opt-in per document (only the replaced lines, its line endings kept); a change made to the file outside relaybay is a new version with a note | done | [0027](design/0027-file-documents-written-and-task-context.md) |
| Sections linked to a card (`task_doc_link`); `task_context` shows them, the open backlog items of the card's subject and only the decisions related to the card | done | [0027](design/0027-file-documents-written-and-task-context.md) |
| `relaybay import --backlog --project`: one project's rows, rows that match existing items are not created again, `--match` by hand | done | [0027](design/0027-file-documents-written-and-task-context.md) |
| Archive: tasks archived by hand are deleted after 30 days with an export first; an archived project is deleted on request; an Archive column on the board | done | — |
| The Stands & access page and the environment tools removed; a project Settings page holds repositories, archive and areas | done | — |
| Shorter server instructions by when they apply; comments and review steps read like a messenger; the dialog keeps its height; the agent's todo under the plan | done | — |
| 0.15.1: backlog "new" means not seen yet; a card shows only the repositories a task linked, a branch in each and PRs named by repository | done | — |
| 0.15.2: update download progress and Cancel in the tray; the tables 0.12 left unread are dropped (migrations 0058, 0059) | done | — |

## 0.16 — the card leads the whole task, change documents per card (released 2026-11-23)

| Item | Status | Design |
|---|---|---|
| Board links of documents and sections in MCP answers (`board_url` for a LAN name); a card whose document field names a file document's file leads to it; a card's local Markdown file becomes its file document; GitHub-style anchors | done | — |
| Card fields: deploy notes with a done mark, a worktree per repository, a cancellation reason, `description_add` | done | — |
| `blocked_by` between cards of any project, a chip on the card, `Blocked by` in `task_context`; a merge order of the test cards with warnings | done | — |
| Deleting a project document with an export first; purged cards answer with their export; an empty URL parameter is no secret | done | — |
| Change documents `spec-<card>`: a delta per section, `changing on <stage>` marks on the spec, hints when a done card still has one | done | — |
| `doc_update(from_path)`: a one-off import of a local file; warnings for links without a target | done | — |
| The task dialog puts what waits for the owner first and leaves out empty blocks | done | — |

## 0.17 — tool sets by role, coordinator mode, the day time log (released 2026-11-24)

| Item | Status | Design |
|---|---|---|
| The day time log across projects: marks on cards or labels, the day's strip and totals on `/`, the card's time, minutes entered in the tracker; coordinator tools `timelog_mark`, `timelog_list`, `timelog_fix`, `worklog_add` | done | [0028](design/0028-day-time-log.md) |
| Tool sets by role at `/mcp/agent`, `/mcp/coordinator`, `/mcp/reader`; coordinator mode with `coordinators` set (agents report findings and questions, the tracker mark and archive are the coordinator's, `waiting_on` as a fixed role); next-call hints; `task_update(stage=…, fields=…)` and `report_add(stage=…)` | done | — |
| Findings and questions as data in reports (`report_add(findings=…, questions=…)`), triaged by the coordinator; `inspect_only` cards; a writing tool refuses an argument it does not take | done | — |
| The card dialog's Approvals right under Next with Approve and Refuse in sight, why a decided request has no button, `approval waits` on the board card; `approval_withdraw` | done | — |
| A repository with a worktree on one line of the Code row | done | — |

## 1.0

Stable MCP contract, complete documentation, a migration path from earlier versions, tested on three
operating systems, and a week of the maintainer's daily work with the board next to the desktop app.

## Later, not planned

- Optional semantic search (`relaybay[semantic]`): moved out of 0.5.0, no corpus justifies it yet;
  [0008](design/0008-memory-and-context.md) enables it only when it beats full-text search on an
  evaluation set.
- A phone app that connects to a local relaybay. Phone access stays through the browser (LAN mode);
  the priority is the local workflow.

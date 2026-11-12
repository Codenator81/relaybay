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
| Desktop session links and binding history, aliases, done window and archive, tracker mark and history, next-step freshness, `task_patch`, slimmer cards and task dialog | done | [SPEC §1–2](SPEC.md#spec-concepts) |
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
| Map (structure and "now" views) and Usage | done | [0010](design/0010-map-and-usage.md) |
| Rules versioning, `rules_update` / `rules_propose`, Settings → Rules, MCP prompts (1.5b–c) | done | [0002](design/0002-working-rules.md) |
| Ctrl+K quick switcher, saved filters | done | [0004](design/0004-navigation.md) |
| Project documents in the database (SPEC, STATUS, TASK) and moving a markdown coordinator setup over | done | [0011](design/0011-docs-in-database.md) |

## 0.5.0 — intake (released 2026-11-05)

| Item | Status | Design |
|---|---|---|
| Intake from chats and e-mail: sources, matching to the waiting task, "answer received", autonomy switch per project and task, example Telegram and e-mail watchers | done | [0015](design/0015-intake-and-autonomy.md) |
| Backlog: findings of sessions and reviews, intake proposals, "To plan" creates the task, `backlog.md` import and export | done | [0015](design/0015-intake-and-autonomy.md#backlog) |

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
| Tray notifications: a task waiting on you, a review arrival, an agent's answer, a server problem, also with the board closed | done | [0020](design/0020-tray-notifications.md) |
| Messages between sessions on the board; stop all and budgets; a fake `claude` and end-to-end tests; a versioned MCP contract; backlog priority and assignee; an Audit page and a per-agent live switch | done | [0017](design/0017-readiness-for-agents-inside.md) |

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

## 0.9 — the runner on a card, questions and approvals

| Item | Status | Design |
|---|---|---|
| The probes of 0012 (resume, permissions in `-p`, the owner's word, concurrent access, bare and subscription terms, token numbers) | done | [0012](design/0012-runner-and-embedded-terminal.md#probes-2026-11) |
| The server ends open wake-up long polls when it stops | done | [0021](design/0021-agents-inside.md#the-pain-it-removes) |
| A run on a card in one repository used in place: the login check, headless turns with `--resume`, model, effort, permission profile, waiting texts, stop with the process tree, end, hooks linked by `RELAYBAY_RUN_ID` | done (PR 1a) | [0021](design/0021-agents-inside.md#the-run) |
| A run over a project folder with a worktree per touched repository (`--add-dir`, the untracked Claude Code files copied in), relaybay's hooks and MCP server added per run when setup did not install them, `run_start` / `run_status` over MCP | in progress (PR 1b) | [0021](design/0021-agents-inside.md#the-run) |
| A run's session opened in the Claude desktop app and handed back; permission mode, model and effort between turns (`run_settings` over MCP lowers only) | in progress (PR 1c) | [0021](design/0021-agents-inside.md#plan-of-09-by-pull-request) |
| Full turn reports with `verified` marks | next | [0021](design/0021-agents-inside.md#the-run) |
| Questions (with "look at this" for visual acceptance) and approvals as objects on the card and in the Inbox | next | [0022](design/0022-questions-and-approvals.md) |
| Slots and free memory per machine, queueing with a reason; the brake and budgets on runs; lifecycle and retention | next | [0021](design/0021-agents-inside.md#the-run) |

## 0.10 — the terminal

| Item | Status | Design |
|---|---|---|
| A Terminal tab on a run: PTY, vendored xterm.js, `claude --resume <id>`, off by default, loopback | later | [0012](design/0012-runner-and-embedded-terminal.md#embedded-terminal) |

## 0.11 — the coordinator and schedules

| Item | Status | Design |
|---|---|---|
| The coordinator as one permanent run woken by events, profiles as data, the Coordinator page | later | [0023](design/0023-coordinator-and-schedules.md) |
| Schedules: coordinator turns, read-only `-p` checks, reminders | later | [0023](design/0023-coordinator-and-schedules.md#schedules) |
| An agents view: who started whom, who writes to whom | later | [0010](design/0010-map-and-usage.md) |

## 0.12 — role profiles

| Item | Status | Design |
|---|---|---|
| Role profiles editable on the board | later | [0023](design/0023-coordinator-and-schedules.md#profiles-as-data) |
| A manager per project and a Managers page, only if a week of coordinator work shows the need (default: not built) | later | [0021](design/0021-agents-inside.md) |
| Agent Client Protocol adapters, containers: not before the runner has run a week | later | [0017](design/0017-readiness-for-agents-inside.md) |

## 1.0

Stable MCP contract, complete documentation, a migration path from earlier versions, tested on three
operating systems, and a week of the maintainer's daily work through relaybay without going back to the
desktop app for anything relaybay should do.

## Later, not planned

- Agent runtime: local process | Docker container (sandbox with the task's repo for `claude -p`,
  report back, container stopped), run by the program on a manager's MCP call; a separate setting
  from the server mode. 0.6 starts no containers.

- Optional semantic search (`relaybay[semantic]`): moved out of 0.5.0, no corpus justifies it yet;
  [0008](design/0008-memory-and-context.md) enables it only when it beats full-text search on an
  evaluation set.
- A phone app that connects to a local relaybay. Phone access stays through the browser (LAN mode);
  the priority is the local workflow.

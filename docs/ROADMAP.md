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

## 0.3.0 — acceptance and memory

Ships when a coordinator runs its daily work from relaybay.

| Item | Status | Design |
|---|---|---|
| Review stage, paged board, Review tab with checklist and screenshots, Review queue in Inbox, browser notification (11.5) | done | [0007](design/0007-review.md) |
| Memory and documents: decisions with scopes, memory notes and blocks, full-text search (11.6a) | done | [0008](design/0008-memory-and-context.md) |
| Digests per level with cursors and budgets, SessionStart context injection (11.6b) | done | [0008](design/0008-memory-and-context.md) |
| Markdown import and export of the memory layout, dry-run diff (11.6c) | done | [0008](design/0008-memory-and-context.md) |
| Task plan and checkpoints: ordered steps with status and checkpoint, draft / accepted plan, the agent's todo list shown under the current step, "work by plan" rule (M/L need an accepted plan before dev, S may go with the agent's todo), the current step reminded through hooks (11.7) | done | [0014](design/0014-task-plan.md) |

## 0.4.0 — insight

| Item | Status | Design |
|---|---|---|
| Screenshot annotations: drawing, numbered pins, flattened PNG for the agent (11.5b) | later | [0009](design/0009-screenshot-annotations.md) |
| Map (structure and "now" views) and Usage | later | [0010](design/0010-map-and-usage.md) |
| Rules versioning, `rules_update` / `rules_propose`, Settings → Rules, MCP prompts (1.5b–c) | later | [0002](design/0002-working-rules.md) |
| Ctrl+K quick switcher, saved filters | later | [0004](design/0004-navigation.md) |
| Project documents in the database (SPEC, STATUS, TASK) and moving a markdown coordinator setup over | later | [0011](design/0011-docs-in-database.md) |

## 0.5.0 — intake

| Item | Status | Design |
|---|---|---|
| Chats and e-mail intake into proposals with approval | later | — |
| Optional semantic search (`relaybay[semantic]`) | later | [0008](design/0008-memory-and-context.md) |

## 0.6+ — agents inside

| Item | Status | Design |
|---|---|---|
| Runner and embedded terminal with the user's own `claude` | later | [0012](design/0012-runner-and-embedded-terminal.md) |
| Agent Client Protocol adapters, role profiles, embedded manager, project managers | later | [0012](design/0012-runner-and-embedded-terminal.md) |
| Coordinator and Managers pages, Schedules in the sidebar | later | [0004](design/0004-navigation.md) |

## 0.7+ — workers

| Item | Status | Design |
|---|---|---|
| Scheduled workers per project | later | — |

## 1.0

Stable MCP contract, complete documentation, a migration path from earlier versions, tested on three
operating systems.

## Later, not planned

- A phone app that connects to a local relaybay. Phone access stays through the browser (LAN mode);
  the priority is the local workflow.

# Design documents

One design per step: goal, model, API, UI, risks, size and status. Status is **Proposed**,
**Accepted** or **Implemented in vX**. When a step ships, its final behaviour moves into
[SPEC.md](../SPEC.md) and the design is frozen. Versions and order: [ROADMAP.md](../ROADMAP.md).

| # | Design | Status | Version |
|---|---|---|---|
| 0001 | [Hooks and machine setup](0001-hooks-and-setup.md) | Implemented in v0.1.0 | 0.1.0 |
| 0002 | [Working rules](0002-working-rules.md) | 1.5a Implemented in v0.1.0; 1.5b–c Accepted | 0.1.0 / 0.4.0 |
| 0003 | [Packaging and release](0003-packaging-and-release.md) | Implemented in v0.1.0 (GIF, repository settings pending) | 0.1.0 |
| 0004 | [Navigation: sidebar, Inbox, Map, Usage, Ctrl+K](0004-navigation.md) | Accepted | 0.2.0 / 0.4.0 |
| 0005 | [Comments to agents](0005-comments-to-agents.md) | Implemented (unreleased) | 0.2.0 |
| 0006 | [Environments, access and agent permissions](0006-environments-and-access.md) | Implemented (unreleased) | 0.2.0 |
| 0007 | [Review: checklist with screenshots](0007-review.md) | Implemented | 0.3.0 |
| 0008 | [Memory and context](0008-memory-and-context.md) | Accepted (11.6a, 11.6b built) | 0.3.0 |
| 0009 | [Screenshot annotations](0009-screenshot-annotations.md) | Implemented (unreleased) | 0.4.0 |
| 0010 | [Map and Usage](0010-map-and-usage.md) | Implemented | 0.4.0 |
| 0011 | [Project documents in the database](0011-docs-in-database.md) | Implemented (unreleased) | 0.4.0 |
| 0012 | [Runner and embedded terminal](0012-runner-and-embedded-terminal.md) | Accepted (direction) | 0.7+ |
| 0013 | [Policy as data](0013-policy-as-data.md) | Implemented (unreleased) | 0.2.0 |
| 0014 | [Task plan and checkpoints](0014-task-plan.md) | Implemented (unreleased) | 0.3.0 |
| 0015 | [Intake from chats and e-mail, autonomy, backlog](0015-intake-and-autonomy.md) | Accepted | 0.5.0 |
| 0016 | [Desktop program: tray, window, autostart, installers](0016-desktop-program.md) | Accepted (PR1 built) | 0.6.0 |

Single decisions with context and consequences are ADRs: [ADR-0001 HTMX + SSE](../adr/0001-htmx-sse.md).

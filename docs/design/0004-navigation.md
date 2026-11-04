# 0004 — Navigation: sidebar, Inbox, Map, Usage, quick switcher

- Status: **Accepted; steps (1) and (2) implemented** (sidebar, Inbox, Sessions, Activity, Map,
  Usage; see [SPEC §2](../SPEC.md#spec-board-nav)). Replaces the planned top tabs (Board | Review | Settings).
- Size and order: (1) sidebar, Inbox, Sessions, Activity ~1.5–2 agent-days, right after the
  documentation restructure (0.2.0); (2) Map ~2 and Usage ~1 after environments and access
  ([0010](0010-map-and-usage.md), 0.4.0); (3) Coordinator, Managers, Schedules with the runner
  ([0012](0012-runner-and-embedded-terminal.md)). Ctrl+K and saved filters ~0.5 (0.4.0).

## Goal

One place to see everything that needs the owner, and a navigation that grows with relaybay without
cluttering the header.

## Layout

A left sidebar with two levels. Items whose feature does not exist yet are hidden.

**Global:**

- **Inbox / Needs you** — everything waiting for the owner: the Review queue
  ([0007](0007-review.md)), tasks with `waiting_on` = the owner, agent questions, rule proposals
  ([0002](0002-working-rules.md)), `ask`-level environment requests
  ([0006](0006-environments-and-access.md)), later intake proposals.
- **All boards** — the projects list.
- **Coordinator** — the desktop coordinator card until the runner exists: status, last reports, "open in
  desktop"; the coordinator comes from config.
- **Map**, **Usage** ([0010](0010-map-and-usage.md)).
- **Activity** — the event feed plus the live-actions journal.
- **Settings** — Integrations, Rules, notifications.

**Projects**, each expandable: Board, Manager (only if one is assigned), Sessions (hook statuses),
Schedules, Stands & access, Docs & rules.

**Phone:** a bottom bar or a drawer instead of the sidebar.

## Model and API

No new tables for step (1): Inbox and Activity are queries over tasks, sessions and events. The
owner's name for `waiting_on` matching comes from config. Inbox counts update live over SSE.
Routes: `/inbox`, `/activity`, `/p/<slug>/sessions`; existing URLs stay.

## Quick switcher and saved filters

- **Ctrl+K**: fuzzy search over tasks (key, title), projects and sessions; Enter opens the task
  dialog, board or session. Not captured inside inputs.
- **Saved filters**: a named board query (stage, waiting on, session, text) stored in the database,
  shown under the project.

## Risks

| Risk | Mitigation |
|---|---|
| Sidebar costs width on the board | Collapsible, remembered per browser; drawer on phones |
| Inbox becomes noise | Only items that need the owner; each links to the place where it is resolved |
| Empty items for unbuilt features | Hidden until the feature ships |

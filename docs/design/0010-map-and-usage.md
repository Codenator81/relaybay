# 0010 — Map and Usage

- Status: **Accepted**, planned for 0.4.0, after environments and access
  ([0006](0006-environments-and-access.md)). Part of the navigation plan
  ([0004](0004-navigation.md)).
- Size: Map ~2 agent-days, Usage ~1.

## Goal

See the whole system at a glance — who works on what, who talks to whom, who touches which
environment — and how much of the subscription each session and project uses.

## Map

**Library:** Cytoscape.js, vendored like htmx (no Node toolchain, no CDN).

**Nodes:** owner, coordinator, managers, sessions, workers, projects and their tasks, environments
(local / test / live), intake sources.

**Edges:** hierarchy; session → task binding; messages (board comments; agent-to-agent messages seen
by the `PreToolUse` hook as the tool name plus its `to` field); session → environment access,
coloured by permission level; intake → proposal → task.

**Badges:** status (working / waiting / idle / ended), unread, waiting for the owner.

**Views:** *Structure* (coordinator in the centre → projects → manager → sessions and workers) and *Now*
(edges of the last N minutes highlighted). Live over SSE: an event updates the affected nodes and
edges without a reload.

**API:** `GET /map/graph?view=structure|now&minutes=N` returns nodes and edges as JSON for the page;
the hook receiver records message edges as events (`session.message`, recipient and time only, never
the message text).

## Usage

Subscription use by session and model, per day and week and per project, read from the Claude Code
transcripts whose paths the hooks report (`transcript_path`). Read-only access to local files; the
totals are cached in the database with the file offset read so far. Nothing leaves the machine.

**UI:** a table and a small chart per day/week; filters by project, session and model; a link from
a session to its usage.

## Risks

| Risk | Mitigation |
|---|---|
| Transcript format changes between Claude Code versions | Tolerant parser, unknown lines skipped, version noted; Usage shows "unknown" instead of guessing |
| Large transcripts slow the server | Incremental reading by offset, in a background task |
| Map unreadable with many nodes | Collapse projects, filter by project, *Now* view shows only recent edges |
| Message edges leak content | Only sender, recipient and time are stored |

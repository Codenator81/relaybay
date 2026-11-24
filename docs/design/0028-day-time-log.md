# 0028 — The day time log across projects

- Status: **Accepted**. Step 1 (data, domain, service, MCP) is in `[Unreleased]`; step 2 (the
  board: the day panel on `/`, the card chip, the dialog's Time row, live updates, a
  `portfolio_digest` line) follows.
- One coordinator works on many projects in a day and fills the external tracker's work log at
  the end of it. relaybay already knows the cards; it now also keeps when work moved from one
  card to another, so the day reads at a glance and the minutes for the tracker come from one
  place.
- Size: about 2 agent-days in two pull requests. One migration (0064), additive. MCP contract
  stays **version 2**: four new tools, nothing changed or removed.

## Why

- The work log is entered per card and day in a tracker outside relaybay. Without a record of the
  day the coordinator rebuilds it from memory and the board's events, which hold when a card
  changed, not when someone worked on it.
- Work without a card (a call, e-mail, a review of someone else's change) belongs to the day too,
  so a mark may name a free label instead of a card.
- relaybay shows the numbers and does not judge them: what counts as work, how it is rounded and
  whether it is billed stay outside.

## The rule

- A **mark** is `start`, `switch` or `pause` at a time. `start` and `switch` name exactly one of a
  card and a label; a `pause` names no card, its label says why.
- Marks are ordered by (time, id). A non-pause mark opens a **span** that ends at the next mark of
  any kind; a pause opens a gap. Today's last span runs until now.
- **Unclosed**: the next mark (any kind) is on a later day and the span is not a short night, or
  a past day's last span has no next mark. It counts 0 on its start day and is listed with the
  hint to add the pause with that day's date. A forgotten pause would otherwise book the evening
  and the night.
- A span over midnight counts only when it ends on the very next day and lasts at most 4 hours
  (late work, 23:30 to 00:30 gives 30 minutes to each day); a span over two midnights never
  counts. The day is the server machine's local date; there is no zone setting (in Docker: UTC
  unless `TZ` is set).
- Durations are summed in seconds and shown floored to minutes as `1h05`, `0h30`.
- The same mark again within 60 s returns the first one; "Working on this now" on the card that
  runs is a no-op. Odd sequences are written with a note, never refused.
- **Corrections** change a mark in place or delete it, with a required reason; the event keeps
  the mark before and after and lands on the Audit page.
- **In the tracker**: an append-only ledger of minutes per card and day. A correction is a
  negative row with a reason, refused when the card's day would go below 0. `tracker_logged` (the
  work log is done) stays a separate mark and is not derived from the ledger.
- In coordinator mode an agent's write is refused (`The day time log is the coordinator's.
  Nothing changed.`); without `coordinators` every caller may write.

## Data

Migration `0064_time_log.sql`: `time_mark` (time, kind, card id and key, project, label, actor,
session) and `tracker_worklog` (card id and key, project, day, minutes, ref, reason, actor,
session). Every reference is `ON DELETE SET NULL` and the key is kept, so a purged card's or a
deleted project's time stays on its days; a deletion's export follows only `CASCADE` keys and
does not hold these rows.

## MCP

`timelog_mark(kind, task?, label?, at?)`, `timelog_list(day?, task?, project?)`,
`timelog_fix(mark, reason, at?, kind?, task?, label?, delete?)` and
`worklog_add(task, minutes, day?, ref?, reason?)`, each write with `session`, `session_title`
and `hook_session`. They are listed on `/mcp` and `/mcp/coordinator` only; the coordinator's
instructions have one line about them, the `/mcp` instructions are unchanged. Events
`timelog.marked`, `timelog.fixed`, `timelog.removed` (with `keys`: the cards whose time changed,
for the board's live update) and `worklog.added`. Reference: [MCP tools](../mcp-tools.md#day-time-log).

## Board (step 2)

- `/`: a day panel above the projects: date navigation, a strip of the day's spans (one colour per
  project, grey for labels, gaps for pauses, dashed for unclosed), the day's total and the totals
  per project, the marks with delete, a form to add a mark and a Pause button.
- A card shows `now 0:35` while it runs, else `today 1h20` when it has time today.
- The task dialog has a Time row `today · total · in tracker` with "Working on this now" and
  "Pause".
- Live: `timelog.*` refreshes the cards named in `keys` and the open dialog; the day panel follows.

## Decided points

- Dedicated tools, not fields on existing writes: the time log is not part of a card's state.
- A past day's last span without a pause counts 0 instead of running to midnight.
- The day follows the server machine's clock; a zone setting can come later without a migration
  (times are stored in UTC).
- The ledger is append-only: corrections are rows, so what was entered and when stays visible.

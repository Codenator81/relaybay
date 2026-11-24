# Keep the day's time log

One coordinator works on many projects in a day and enters the work log in an external tracker
at the end of it. The day time log (since 0.17) keeps when the work moved from one card to
another, so the day reads at a glance and the minutes for the tracker come from one place.
relaybay shows the numbers and does not judge them: what counts as work, how it is rounded and
whether it is billed stay with you. Behaviour in detail: [SPEC](../SPEC.md#spec-time-log), design
[0028](../design/0028-day-time-log.md).

## Marks and spans

A **mark** is `start`, `switch` or `pause` at a time. `start` and `switch` name a card or a free
label (a call, e-mail, someone else's review); a `pause` names no card. A mark opens a **span**
that runs until the next mark; a pause opens a gap. Today's last span runs until now.

A span that runs into the next day and is longer than a short late evening (at most 4 hours over
midnight) is **unclosed**: it counts 0 and is listed with the hint to add the pause with that
day's date, so a forgotten pause does not book the night. The day is the server machine's local
date (in Docker: UTC unless `TZ` is set).

## On the board

- **`/` opens with the day**: its total, a strip of the spans coloured by project (labels grey,
  pauses as gaps, unclosed spans faint with a dashed edge), totals per project, what runs now, the
  marks with **Delete**, a form to add a mark and **Pause**. Arrows move to another day.
- **A card** shows `now since 13:50 · today 2h10` while it runs, or `today 1h20` when it had time
  today.
- **The task dialog** has a **Time** line under the session's state: `today · total · in tracker`,
  with **Working on this now** (start, or switch from the card that runs) and **Pause**.
- **A project row** on `/` shows its time today, in total and in the tracker.

## From a coordinator session

The tools are listed on `/mcp` and `/mcp/coordinator`
([MCP tools](../mcp-tools.md#day-time-log)):

| When | Call |
|---|---|
| You start on a card, or switch to another | `timelog_mark(kind="switch", task="acme-shop-12")` |
| Work without a card | `timelog_mark(kind="switch", label="call with the customer")` |
| Lunch, the end of the day | `timelog_mark(kind="pause")`, or with `at="17:40"` afterwards |
| Look at the day | `timelog_list()`, `timelog_list(day="2026-11-24")`, `timelog_list(task="acme-shop-12")` |
| A mark was wrong | `timelog_fix(mark=17, reason="started later", at="10:15")`, or `delete=true` |
| Minutes entered in the tracker | `worklog_add(task="acme-shop-12", minutes=90, ref="ENG-12")` |

`timelog_list` answers with the day's spans and durations (`1h05`), totals per project, card and
label, what runs now and the unclosed spans. A correction keeps the mark before and after on the
Audit page. The tracker minutes are an append-only ledger: a correction is a negative row with a
reason. The work-log mark of a card (`tracker_logged`) stays a separate field.

`portfolio_digest` has one line for today's time.

## In coordinator mode

With `coordinators` set in the server's config, an agent's write to the time log is refused
(`The day time log is the coordinator's. Nothing changed.`): the coordinator marks where you are.
Without `coordinators` every caller may write. See
[Coordinator and agents workflow](coordinator-workflow.md#coordinator-mode).

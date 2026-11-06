# 0020 — Tray notifications

- Status: **Implemented (unreleased)**, for 0.8.0. The behaviour is in
  [SPEC §13](../SPEC.md#spec-desktop-notify). Item of [0017](0017-readiness-for-agents-inside.md)
  "before the runner".
- Size: one pull request, about 2 agent-days. No migration.

## Problem

Browser notifications ([SPEC §2](../SPEC.md#spec-board-notify)) need an open board tab that was
given the permission. The owner runs the desktop program with its window hidden: a task can wait
on him for an hour with nothing on the screen.

## Decisions

- **The server decides what is news, the program decides how to show it.** A pure
  `domain/notifications.py` turns an event into a notice (`classify`). `GET /api/notifications`
  (Bearer) reads the events after a cursor. `desktop/notifier.py` polls it, filters by
  `notify_kinds`, groups and shows. The filter is on the program's side, so the kinds are a setting
  of the program and the server needs none.
- **Polling, one path.** Every 5 s over HTTP, loopback without a proxy as `healthz`, for the embedded
  and an external server alike. A thread reading `Service` directly would work only in embedded mode.
  An idle poll reads one `MAX(id)`.
- **The cursor is an event id** in `notify_state.json` in the config folder, machine-written like
  `window-state.json`. A first start has no cursor and learns the newest id: no replay of history.
  A cursor belongs to one board (its base URL and data folder): another board starts from its newest
  event. A cursor past the newest event (another database behind one address) moves back.
- **Paging.** `limit` counts the events read, not the notices, and `last_id` always moves on, so a
  page of events that are no notice cannot stall the reader. The program reads until `last_id` stops
  moving, which is why a long downtime ends in one summary, not in a toast per page.
- **Grouping.** `icon.notify` replaces the previous balloon, so a burst would lose all but the last.
  At most one toast per 20 s window, the first at once: one item with its own title and text, several
  as "3 need you: 2 reviews, 1 reply". The same task and kind collapse inside the window, keeping the
  newest wording. A move to review that also sets `waiting_on` is two events and one thing to do:
  `review` absorbs `waiting` of the same task.
- **The owner's own writes are not news:** actors `board`, `owner` and the `owner_names`.
- **Quiet mode** is a setting, not a state: a tray checkbox writes `notify_quiet`. Held items live in
  `notify_state.json`, so a restart in quiet mode keeps them and the dot.
- **`notify = false`** stops the polling and forgets the cursor: switching it on later starts from the
  newest event, not with a summary of the time it was asked to be silent.
- **A click cannot open anything.** pystray's Windows backend handles only clicks on the icon, not on
  a balloon. Instead the tray has **Open Inbox**. The API's `url` is there for a client that can.
- **A poll that fails changes nothing.** The cursor stays, one log line says paused and one resumed.
  The watchdog is what tells the owner about a server that is gone: `_watch_external` adds one notice
  of the kind `server` on becoming unreachable. The embedded "keeps failing" notice stays as it was
  and is not subject to the settings, because the problem page comes with it.

## Kinds

| Kind | From | Default |
|---|---|---|
| `waiting` | `task.updated` with `changes.waiting_on` naming an owner, matched as the Inbox does | on |
| `review` | `task.stage_changed` to `review` or `test` | on |
| `reply` | `comment.added` with `author_kind == "agent"` | on |
| `answer` | `intake.answer_received` | on |
| `permission` | `session.status_changed` to `waiting` (the `Notification` hook) | off |
| `brake` | `brake.set` by an agent or a rule, not a budget's own (`budget` tells it) | on |
| `budget` | `budget.warned`, `budget.exceeded` | on |
| `server` | the program's watchdog, local | on |

`permission` is off because the `Notification` hook also fires for an idle prompt, and a working
day has many of them.

## Risks

A board tab with browser notifications allowed tells a review twice (documented in SPEC; they are
different channels and the program cannot see the tab) | the balloon's limits (63 and 255 UTF-16
units, pystray raises beyond them: `fit` cuts by units, not characters) | pystray's X11 backend has
no notifications, and that is the backend of the `.deb`: there a notice is only a log line |
Focus Assist or Do not disturb hide balloons with no way to tell | the text of a toast (a task title,
the first words of a reply) shows on the desktop, also when others look at it: quiet mode or
`notify_kinds` | macOS and Linux are CI smoke only.

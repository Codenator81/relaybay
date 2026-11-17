# 0025 — The board with the desktop

- Status: **Accepted**, 2026-11-17, for 0.12 (by the coordinator with the defaults of the
  [decided points](#decided-points)). Supersedes [0012](0012-runner-and-embedded-terminal.md),
  [0021](0021-agents-inside.md), [0023](0023-coordinator-and-schedules.md) and the agent card,
  thread and Coordinator page of [0024](0024-card-modes-thread-and-areas.md); narrows
  [0022](0022-questions-and-approvals.md) to desktop sessions. Changed by the owner on 2026-11-17:
  reminders and the Schedules page are removed too ([R3](#decided-points)); so are the brake,
  token budgets, the Usage page and the usage reader, intake and messages between sessions (rows
  11, 17, 18 and 19).
- Owner's decision of 2026-11-17: **agents inside the program stop.** relaybay is the board that
  coordinates tasks next to the Claude desktop app. The agent's window, its output and the talk
  with it stay in the desktop app, which does them better than a board can; relaybay is the staff
  room above it. 0.12 is a refactoring release.
- Size: about 5 agent-days in five pull requests ([plan](#plan-by-pull-request)). One migration
  (0049), data only; no table is dropped in 0.12.

## Why

0.9 to 0.11 put the owner's own `claude` inside the program: a run on a card, a terminal, a
coordinator run, schedules that start turns. The first real runs showed the cost. To be usable the
card has to show the agent's work and take the owner's words as well as the desktop app does, and
the desktop app is a moving target built by the people who build the agent. Every hour spent there
copies a window that already exists, and the copy stays behind.

What the desktop app does not do is what relaybay did before 0.9 and still does best: one picture
over all sessions and projects, who waits for whom, reports and acceptance by steps, rules, memory,
a guard, comments that reach a session. 0.12 returns to that and removes the rest.

Rule for the inventory: **what only a run inside the program needs is removed, not hidden** (the
git history keeps it); what a desktop session also uses stays.

## Inventory

| # | Part (since) | Decision | Why |
|---|---|---|---|
| 1 | Runs on a card: the runner (`runner/`), turns with `--resume`, worktrees per run, the Agent block and its routes, run events and their retention, handing a run to the desktop app (0.9) | **remove** | the core of agents inside |
| 2 | Slots and the free-memory check for turns, "Start anyway" (0.9) | **remove** | they queue only runs. `max_concurrent_sessions` (0.8, warns about desktop sessions) stays |
| 3 | The Terminal tab: PTY, vendored xterm.js, the WebSocket, `pywinpty` and `wsproto` (0.10) | **remove** | it opens a run's session; two dependencies and a WebSocket surface leave with it |
| 4 | The coordinator run: its folder and starter `CLAUDE.md`, wakes by events, compaction, its budget, the Coordinator page and the sidebar mark (0.11) | **remove** | a permanent run. The **coordinator role** stays: a desktop session named in `coordinators` still may `rules_update`, `plan_accept`, `task_archive` |
| 5 | Permission profiles, Settings → Profiles, project run defaults (0.11) | **remove** | they only shape a run's flags and board rights |
| 6 | Settings → Agents (which `claude`, the sign-in check, run limits, coordinator settings) (0.9) | **remove** | nothing left to set. `claude_home` stays (setup, doctor) |
| 7 | Schedules: coordinator turns and read-only `-p` checks (0.11) | **remove** | both start a turn |
| 8 | Schedules: **reminders** (a tray notice and an Inbox line at a time, no model) and the Schedules page with its sidebar item (0.11) | **remove** (owner's decision of 2026-11-17) | schedules go entirely; one kind left alone would still keep a timer, a page and three tools |
| 9 | Questions as objects: `question_ask`, the Inbox, "look at this", the default after a deadline (0.9) | **keep for sessions** | a desktop session asks the same way; the answer reaches it as an owner comment through the hooks, as today. The road "answer as a run's turn" is removed |
| 10 | Approvals as objects: `approval_request`, the guard's hold, the reservation for one call (0.9) | **keep for sessions**, as they are | they already work for desktop sessions when the policy's `approvals.desktop` is on (off by default, unchanged). The origin "a run's permission denial" is removed |
| 11 | The brake and token budgets (0.8): the banner, Stop all, the refusals and budget lines through the hooks, the Budgets block, the task's budget row, budget notifications | **remove** (owner's decision of 2026-11-17) | built for agents on cards; the desktop app shows the plan limits. The per-session "No live now" switch and the guard stay: they are access, not the brake |
| 12 | `verified:` marks on reports and the rules warning (0.9) | **keep** | `report_add` of any session |
| 13 | Manual archive of done cards (0.9) | **keep** | board |
| 14 | Tray notifications, plan, review, the backlog, memory, policy, guard (0.2 to 0.8) | **keep** | the board |
| 15 | The fake `claude` and its end-to-end tests (0.8) | **keep what tests hooks and MCP**, remove the stream-json part | the hook contract is still tested against it |
| 16 | Areas and the home icon (0.12, PR 1 of 0024) | **keep** | needed in any mode |
| 17 | The Usage page and its sidebar item, the background reader of transcripts and the transcript paths kept only for it (0.4) | **remove** (owner's decision of 2026-11-17) | the desktop app shows the plan limits; tokens were read only for this page and the budgets |
| 18 | Intake from chats and e-mail: watchers posting with `intake_post`, the Inbox groups Intake and Answers, the "answer received" mark, source trust and task and project autonomy (Settings → Intake), Forward to the agent, the task's Intake block, wake-ups through hooks, intake notifications, `examples/intake/` (0.5) | **remove** (owner's decision of 2026-11-17) | without relaybay's own coordinator nothing can tie a customer's message to a task (customers write no keys, one chat holds many tasks); the coordinator session reads the chats and updates the cards itself. The backlog stays, with proposing a task by hand or over MCP |
| 19 | Messages between sessions: the Messages page and its sidebar item, the task's Messages block, delivery through hooks, their limits, notifications and Inbox lines (0.8) | **remove** (owner's decision of 2026-11-17) | sessions in the desktop app message each other through the desktop app itself |

Nothing is hidden behind a switch: a switched-off half of the program would still have to be
migrated, tested on three systems and explained.

## What gets stronger

The card of a task worked by a desktop session (the session card of 0024), and nothing that copies
the desktop app:

1. **A state line on the card**, large, derived, never set by hand: **Working** (with the
   session's last activity), **Idle**, **Waits for you** (an open question or approval, a review,
   or "waiting on" names the owner), **Ended**, **No session**. One action in the line: **Open**,
   which follows where the session runs ([R10 to R12](#decided-points)): a desktop session opens in
   the desktop app (the link the card has today, made the main button), an ended terminal session
   is resumed with `claude --resume` in a new terminal window on the machine itself, a live
   terminal session is not opened. A disabled or missing Open says why in one line. The same word
   shows on the board card next to the session's dot.
2. **Start or continue from the card.** A card without a live session gets **Copy the brief**: the
   text a new session needs (the card's key, description, decisions, plan and its current step,
   done so far, next step, attachments by path, and the line that tells the session to bind
   itself to the card), built by the function that wrote a run's first turn. The owner pastes it
   into a new session in the desktop app; the session binds itself with `task_bind_session`.
   relaybay starts nothing.
3. **One order of the card**: state line, the last report as the main content, the comment thread,
   then folds (the session and its history, task details). The comment field stays at the bottom
   and outside every refetched fragment, so a live event never clears a half-typed text.
4. **Why a comment has not arrived.** "Not delivered yet" names its reason: no session bound, the
   session ended, the session is idle and the wake-up hook is off. The Inbox lists comments
   waiting longer than an hour.

Not in 0.12: opening a new desktop session with the brief already in it (whether the desktop app's
link can carry a prompt is not known; a probe, later), a view of who started whom.

## Data

Nothing of the board is lost and no table is dropped in 0.12.

- Reports that turns of runs wrote are ordinary `report` rows and stay on their cards. Questions,
  approvals, comments, plans and reviews stay.
- Migration **0049**, data only: every open run is ended with the reason "runs inside the program
  were removed in 0.12" and its waiting turns are cancelled; open questions and approvals asked by
  a run are withdrawn with the same reason (their asker is gone); every open schedule (coordinator
  turns, checks and reminders) is cancelled.
- The tables `run`, `run_turn`, `run_event`, `run_worktree`, `run_profile`,
  `project_run_defaults`, `coordinator_wake`, `schedule`, `schedule_run` and the run columns of
  `question` and `approval` **stay in the file, unread**. Dropping them is a later release's migration, after the
  owner's word; until then a return to 0.11 from the backup taken before 0.12 loses nothing.
- Session rows of runs stay as ended sessions in the history of their cards.
- On disk relaybay touches nothing: worktrees that runs created, Claude Code's transcripts, the
  coordinator's folder. The first start of 0.12 writes one Inbox line that lists the worktrees
  runs left behind with their paths, to be removed with `git worktree remove` when not needed.
- A start on a new version backs the database and the settings up before migrating, as since 0.6.
- `config.toml` keys that lose their meaning (`max_running_turns`, `min_free_memory_mb`,
  `claude_path`, `usage_enabled`, `usage_interval_seconds`) are accepted and ignored; `relaybay
  doctor` names them once.
- The tables of usage, budgets, the brake, intake and session messages stay in the file, unread,
  like the run tables; 0049 also clears every "answer received" mark of intake.
- The hooks installed by `relaybay setup` stay the same commands; no new setup run is needed. The
  `RELAYBAY_RUN_ID` link of a run's hooks is removed.

## MCP contract

The contract stays **version 1** in 0.12; no tool or parameter disappears, so a bound desktop
session with a cached schema keeps working.

- `run_start`, `run_status`, `run_events`, `run_settings`, `schedule_add`, `schedule_list`,
  `schedule_cancel`, `usage_summary`, `budget_get`, `brake_set`, `brake_status`, `intake_post`,
  `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`,
  `message_send`, `messages_list` and `message_ack` stay registered with unchanged parameters as
  **deprecated tools that do nothing**, and so does the resource `relaybay://intake/{source}` (a
  resource cannot go within contract v1). Each answers `Nothing done: this tool does nothing since
  0.12.` and a notice per feature, which also ends its description:

  | Tools | Notice |
  |---|---|
  | `run_start`, `run_status`, `run_events`, `run_settings`, `schedule_add`, `schedule_list`, `schedule_cancel` | `Deprecated: runs and schedules inside the program were removed in 0.12; a card is worked by a desktop session bound to it.` |
  | `brake_status`, `brake_set` | `Deprecated: the brake was removed in 0.12; stop a session in the Claude desktop app.` |
  | `usage_summary`, `budget_get` | `Deprecated: token budgets and the usage reader were removed in 0.12; the Claude desktop app shows the plan limits.` |
  | `intake_post`, `intake_list`, `intake_tie`, `intake_dismiss`, `intake_propose`, `intake_sources`, the resource `relaybay://intake/{source}` | `Deprecated: intake was removed in 0.12; the coordinator session reads the chats and updates the cards itself.` |
  | `message_send`, `messages_list`, `message_ack` | `Deprecated: messages between sessions were removed in 0.12; sessions message each other through the Claude desktop app.` |

  They are listed under "Deprecations" of the contract. They are removed, and the contract becomes version
  2, in the first release that is both a later minor version and 30 days after 0.12, as the
  contract's own rule says.
- `question_ask`, `question_list`, `question_withdraw`, `approval_request`, `approval_list` and
  the other tools are unchanged. The MCP instructions lose their points about the brake and about
  session messages. `area` of PR 1 stays.
- Said plainly: the contract promises an announcement before a removal, and here the behaviour of
  twenty tools goes in the same release that announces it. Runs existed for five days (0.9 was
  released on 2026-11-13; schedules came with 0.11) and could be started only by the owner or a
  coordinator session, so no one else's setup depends on them (the brake, budget, usage, intake and message tools
  only read or stop what this release removes); keeping a runner alive for 30 days only to honour the notice
  would keep everything this design removes.

## Documents

- The project's description follows the decision everywhere, in one pull request of its own
  (owner, 2026-11-17): README (the subtitle, "Why relaybay", the status, the story of the missing
  yes told without agents inside), the pages of the documentation site, the one-line description
  of the repository on GitHub and of the package (`pyproject.toml`, so PyPI shows it with
  0.12.0). The README text and the one-line description are the owner's to accept before the
  merge.
- SPEC loses the sections of runs, the terminal, the coordinator run and profiles and gains the
  session card; `docs/reference` is regenerated; CHANGELOG has a "Removed" list with one line of
  why.
- ROADMAP: the rows of 0.9 to 0.11 are marked "removed in 0.12"; "After 0.11", 1.0 and "Later"
  lose the runner, the coordinator run and containers; the 1.0 line "a week without going back to
  the desktop app" becomes "a week of the maintainer's daily work with the board next to the
  desktop app".
- Designs 0012, 0021, 0023: status "Superseded by 0025", frozen as history. 0022: "Implemented,
  narrowed to sessions by 0025". 0024: "Superseded by 0025; areas and the top bar implemented in
  0.12".

## Risks

- **A large removal breaks what stays.** `service.py` and `routes.py` hold the run code next to the
  board's. The removal goes in two pull requests from the outside in (first what stands on runs,
  then runs), each with the whole suite green on three systems, and a reviewer reads both for
  anything a desktop session path lost.
- **An open run at the upgrade** is ended by the migration; its worktree and its Claude session
  stay, and the session can be opened in the desktop app and bound to the card.
- **Questions withdrawn by the migration** were asked by runs that no longer exist; the card keeps
  them readable with the reason.
- **Installers.** Dropping `pywinpty` and the vendored terminal changes the frozen builds; the
  three installer jobs must pass before the release.

## Decided points

| # | Point | Default | Other way |
|---|---|---|---|
| R1 | Run tables | stay unread in 0.12, dropped later on the owner's word | dropped in 0.12 after the backup |
| R2 | MCP run tools | deprecated stubs, contract 1 | removed now, contract 2 |
| R3 | Reminders and the Schedules page | remove with the other schedules (changed from "keep" by the owner, 2026-11-17) | keep |
| R4 | Questions and approvals | keep for desktop sessions; `approvals.desktop` stays off by default | approvals for desktop sessions on by default |
| R5 | Open questions and approvals of runs at the upgrade | withdrawn with a reason | left open |
| R6 | Worktrees left by runs | listed once in the Inbox, not touched | removed by the program |
| R7 | "Copy the brief" | in 0.12 | later |
| R8 | The 1.0 line in ROADMAP | reworded as above | the owner words it |
| R9 | README and the one-line description | a pull request of their own; the text waits for the owner's yes | inside the removal pull request |
| R10 | The state line's action | one **Open** that follows the session's surface: the desktop link for a desktop session, a new terminal window running `claude --resume <id>` in the session's folder for an ended terminal session (owner, 2026-11-17) | "Open in the desktop app" only |
| R11 | How relaybay knows the surface | the hook sends `CLAUDE_CODE_ENTRYPOINT` with `SessionStart` (`claude-desktop…` desktop, `cli` terminal) and a desktop id means desktop; the session is asked to say it with `task_patch(session_surface)` only when the hook cannot tell (owner, 2026-11-17) | always ask the session |
| R12 | A live terminal session | not opened: the button is disabled with "running in a terminal"; the board cannot find that window, and a second process on a live session is unsafe (owner, 2026-11-17) | open a second terminal on it |

## Plan by pull request

| PR | What | Review | Size |
|---|---|---|---|
| A | This design; statuses of 0012 and 0021 to 0024; ROADMAP; the designs index | coordinator | 0.3 |
| B | Remove what stands on runs: the Terminal tab and its dependencies, the coordinator run with its wakes and page, schedules of every kind and the Schedules page, permission profiles | reviewer: what a desktop session path lost | 1.5 |
| C | Remove runs: the runner, the Agent block, Settings → Agents, limits, the run roads of questions and approvals; the brake, budgets, the Usage page and the usage reader; MCP stubs; migration 0049; ignored config keys; SPEC sections, reference, CHANGELOG | reviewer: migration, MCP contract, hooks | 2 |
| C2 | The project's description: README, ROADMAP, the SPEC's overview and the site's pages, the package's and the repository's one line | the owner (text) | 0.5 |
| D | The session card: state line with one Open by the session's surface (migration 0050, the hook's entrypoint, `session_surface` in `task_patch`, resuming an ended terminal session), order, the comment field outside the refetch, Copy the brief, the state on the board card, the reason of an undelivered comment and the Inbox line for comments waiting over an hour | reviewer: the terminal endpoint | 1.3 |
| E | Release 0.12.0 | — | 0.3 |

PR 1 of 0024 (areas, the home icon) is merged before B.

## Acceptance (0.12)

On the installed program after the update from 0.11: the board, cards, reports, comments, reviews
and plans as before; no Agent block, Terminal, Coordinator page, Schedules page, Usage page, brake banner, Messages page, Intake settings, Profiles or
Agents settings; a card
with a bound session shows its state line and opens the session in the desktop app; a card without
one gives a brief to paste, and the new session binds itself; a comment reaches a session as
before; two cards told apart by their areas; a run that was open before the update shows as ended
with the reason.

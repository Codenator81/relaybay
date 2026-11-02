# MCP tools

relaybay serves MCP over Streamable HTTP at `http://127.0.0.1:8765/mcp`. Every request needs
`Authorization: Bearer <token>` (`relaybay token` prints it). Results are short markdown meant to be
read by the model; failures (unknown task, version conflict, invalid stage) come back as tool errors
with a message that says what to do.

Conventions:

- **Task reference** — the key (`acme-shop-12`) or the numeric id.
- **Stages** — `plan`, `dev`, `test`, `live`, `done`. Any transition is allowed; the event log keeps
  the history.
- **`session`** — optional on writes: who is calling. Pass the Claude desktop app session id
  (`local_<uuid>`) if the session knows it, together with `session_title` (its display name);
  otherwise a human-readable name. relaybay creates the session on first use and records it as the
  actor of the event. It is for display only; the token is the only authorization.
- **Session aliases** — if you don't know your `local_` id, don't rebind a task: the coordinator
  binds you; pass your session name only as `session` on writes. When `task_bind_session` gets a
  plain name for a task bound to a desktop session, the desktop binding (and its "Open in desktop"
  link) is kept and the name is recorded as that session's alias (event `session.alias_added`).
  From then on the name resolves to the desktop session: writes are recorded as it, `task_list`
  and `task_unbind_session` accept the name. Binding by another `local_` id still rebinds; a plain
  name on a task without a desktop session binds as before. Aliases are global: one name maps to one
  session across all projects, and later binds by that name move it to the desktop session bound to
  that task. Because an alias would hijack a plain session of the same name, none is created when
  such a session exists; the call then changes nothing and answers "already bound to <title>
  (local_…); pass your local id".
- **Claude desktop app session links** — for a desktop session id, relaybay builds a link
  (`claude://claude.ai/epitaxy/<id>` by default, see `desktop_link_template` in the README) that the
  board shows as "Open in desktop" and that `task_get` prints. Plain names have no link.
- **Clearing a field** — in `task_update`, an omitted field stays unchanged; `"-"` or an empty
  string clears it (models cannot always send an empty string, so `"-"` is the reliable form). In
  `task_create`, `"-"` leaves a field empty; in `report_add`, `progress="-"` clears the summary.
- **Description and progress** — two markdown fields per task. `description` says what to do and
  why (1–5 sentences). `progress` is a running summary of everything done so far, not the last
  step: the session rewrites it as it goes, ideally with every report (`report_add(progress=…)`).
  The board shows both in full in the task dialog (not on the card).
- **Next step and waiting on** — keep them current: `task_set_stage` and `report_add` take
  optional `next_step` (what happens next) and `waiting_on` (who acts next), saved in the same
  transaction as the stage change or the report; omitted, they stay; `"-"` or `""` clears them.
  A next step written before the task's last stage change is *stale*: the card and the task dialog
  show it muted with a "stale" marker, `task_get` and `project_snapshot` append `(stale)`. Writing
  it again, even with the same text, confirms it as current (a `task.updated` with a version bump). On the board, saving the edit form
  confirms it only when the next-step text itself was changed.
- **External tracker** — `tracker_ref` (issue key, e.g. `ENG-12`, up to 100 characters),
  `tracker_name` (any tracker, free text), `tracker_url` (optional, `http://` or `https://` only;
  anything else is refused) and `tracker_logged` (bool: the work log is entered there). The card
  shows "<tracker_name> <tracker_ref>" as a badge whenever a key is set, a link with a URL, with ✓
  when logged. With the server setting `require_tracker_log = true`, a done task that is not
  logged shows "not logged" (also in `task_get` / snapshots) and is never archived.
- **Tracker history** — every issue key a task has had is kept: setting a different
  `tracker_ref` appends an entry (earlier ones stay, clearing the key keeps them too), and marking
  `tracker_logged` stamps "logged <date>" / "unlogged <date>" on the current entry (logging again
  restarts its mark). `task_get` lists the entries newest first; snapshots and the card show only
  the current issue.
- **Automatic unlog** — a logged task that gets new work loses the mark in the same transaction,
  with an event `task.tracker_unlogged` (payload `reason`): `report_add`, a changed `progress`, or
  a move back out of `live` or `done` to an earlier stage. Metadata edits (title, branch,
  waiting on, next step, other tracker fields) keep it; passing `tracker_logged` in the same write
  wins. A new `tracker_ref` starts as not logged unless `tracker_logged` is passed in the same
  write (a plain `task.updated`; the previous issue's entry keeps its "logged" date). Mark it again
  once the log covers the new work.
- **Done window** — the board's `done` column shows tasks closed in the last `done_window_days`
  (default 7); older ones are archived on the board only (with `require_tracker_log`, not-logged
  done tasks stay).
  `project_snapshot` and `task_list` are unaffected: `include_done` returns all done tasks.

## Reading

| Tool | Arguments | Returns |
|---|---|---|
| `project_list` | — | Table of projects with task counts per stage. |
| `project_snapshot` | `project`, `include_done?` | Open tasks grouped by stage (waiting on, next step, branch, session name and desktop id, version, the start of the description and progress, last report) and recent decisions. |
| `task_list` | `project?`, `stage?`, `waiting_on?`, `session?` (id or name), `include_done?` | One line per task. Done tasks are left out unless `stage` or `include_done` is given. |
| `task_get` | `task`, `reports?` (default 5) | Ends with "Editable fields: …". All fields (a stale next step ends with `(stale)`), description and progress in full, current `version`, the bound session (name and desktop id, link), the tracker issue and work-log mark, session binding history, tracker history, latest reports with their bodies, decisions. |
| `events_since` | `since_id?` (default 0), `project?`, `limit?` (default 50, max 500) | Events oldest first and the `since_id` for the next call. |

## Writing

| Tool | Arguments | Effect |
|---|---|---|
| `project_upsert` | `slug`, `name?`, `repo_path?`, `description?`, `session?`, `session_title?` | Creates the project, or updates the given fields. Slugs: lowercase letters, digits, dashes. |
| `task_create` | `project`, `title`, `stage?` (default `plan`), `waiting_on?`, `next_step?`, `branch?`, `doc_path?`, `description?`, `progress?`, `tracker_ref?`, `tracker_name?`, `tracker_url?`, `tracker_logged?` (default false), `session?`, `session_title?` | Creates a task with the next key of the project (`acme-shop-1`, `acme-shop-2`, …). With `session`, the task is bound to it. |
| `task_update` | `task`, `title?`, `waiting_on?`, `next_step?`, `branch?`, `doc_path?`, `stage_note?`, `description?`, `progress?`, `tracker_ref?`, `tracker_name?`, `tracker_url?`, `tracker_logged?`, `expected_version?`, `session?`, `session_title?` | Patches fields; `"-"` or `""` clears a text field. With `expected_version`, fails instead of overwriting when the task changed meanwhile. |
| `task_patch` | `task`, `fields` (JSON object), `expected_version?`, `session?`, `session_title?` | Changes any editable task fields with the same validation as `task_update` / `task_set_stage` (a `stage` value moves the task, auto-unlog applies). Unknown names fail with the list of allowed ones; `"-"`, `""` or `null` clears an optional text field. The allowed fields: the end of `task_get` or `relaybay://schema/task`. |
| `task_set_stage` | `task`, `stage`, `note?`, `next_step?`, `waiting_on?`, `session?`, `session_title?` | Moves the task; `note` is shown next to the stage. Moving to `done` sets the closed time. `next_step` and `waiting_on` are saved in the same transaction (`task.stage_changed` + `task.updated`, one version bump); `"-"` clears them. |
| `task_bind_session` | `task`, `session`, `session_title?` | Shows the session in the task dialog as the one working on the task (a desktop session also gets an "open in desktop" icon on the card). The previous binding is closed and kept in the task's history (event `task.session_bound`). A plain name on a task bound to a desktop session keeps that binding and becomes its alias (see *Session aliases*). |
| `task_unbind_session` | `task`, `session?` | Removes the bound session from the task; the binding is closed and kept in the history (event `task.session_unbound`). With `session` (desktop id or name), fails without changes if another session is bound. Nothing bound: says so, changes nothing. |
| `report_add` | `task`, `body`, `progress?`, `next_step?`, `waiting_on?`, `session?`, `session_title?` | Appends a markdown report. Its first line is the report summary; a first line `state · size · gist` also sets the state (`inspected`, `changed`, `verified`, `committed`, `pushed`, `blocked`) and size (`S`, `M`, `L`). `progress` replaces the task's progress summary in the same transaction (`task.updated` + `report.added`); omitted, the summary stays; `"-"` clears it. `next_step` and `waiting_on` work the same way and go into the same `task.updated`. |
| `decision_add` | `project`, `text`, `task?`, `decided_by?`, `session?`, `session_title?` | Records a decision; it appears in the project snapshot. |

Writes that change a task increment its `version`. Every change appends an event and updates open
boards immediately.

## Resources

`relaybay://project/{slug}` — the same markdown as `project_snapshot`, so a session can attach a
project snapshot as context.

`relaybay://schema/task` — the editable task fields as a markdown table (field, type, description,
how to clear it), generated from the same list the server validates writes against.

## Schema changes and long-running sessions

An MCP client keeps the parameter schema of a tool it has already loaded. A Claude Code session
that was running before relaybay was upgraded can reconnect after a server restart and see new
tools, while an existing tool (such as `task_update`) keeps its old parameter list, so a new field
looks unavailable. Two ways out:

- Use `task_patch(task, fields={...})` for fields your client does not know yet: `fields` is a
  plain JSON object, validated on the server with the same rules as `task_update` and
  `task_set_stage`. The allowed names are listed at the end of `task_get` ("Editable fields: …")
  and in the resource `relaybay://schema/task` (name, type, description, how to clear).
- Refresh the schemas: reconnect the server in the client (`/mcp` in the Claude Code CLI) or start
  a new session.

## Example

```text
project_upsert(slug="acme-shop", name="Acme Shop")
task_create(project="acme-shop", title="Checkout page", branch="feat/checkout",
            description="Let customers pay by card without leaving the shop.",
            session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e", session_title="Checkout")
  → Created **acme-shop-1** “Checkout page” · stage plan · version 1.
task_set_stage(task="acme-shop-1", stage="dev", next_step="Card form", waiting_on="-",
               session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e")
report_add(task="acme-shop-1", body="verified · M · checkout works\n\nTests pass.",
           progress="- Cart with totals\n- Card form with validation\n- Tests",
           next_step="Review the PR", waiting_on="reviewer",
           session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e")
  → Report #1 added to **acme-shop-1** (verified · M): checkout works
task_unbind_session(task="acme-shop-1", session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e")
  → Unbound Checkout (local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e) from **acme-shop-1** “Checkout page” · stage dev · version 4.
```

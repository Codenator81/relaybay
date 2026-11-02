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
- **Claude desktop app session links** — for a desktop session id, relaybay builds a link
  (`claude://claude.ai/epitaxy/<id>` by default, see `desktop_link_template` in the README) that the
  board shows as "Open in desktop" and that `task_get` prints. Plain names have no link.
- **Clearing a field** — in `task_update`, an omitted field stays unchanged and an empty string
  clears it.

## Reading

| Tool | Arguments | Returns |
|---|---|---|
| `project_list` | — | Table of projects with task counts per stage. |
| `project_snapshot` | `project`, `include_done?` | Open tasks grouped by stage (waiting on, next step, branch, session name and desktop id, version, last report) and recent decisions. |
| `task_list` | `project?`, `stage?`, `waiting_on?`, `session?` (id or name), `include_done?` | One line per task. Done tasks are left out unless `stage` or `include_done` is given. |
| `task_get` | `task`, `reports?` (default 5) | All fields, current `version`, the bound session (name and desktop id, link), session binding history, latest reports with their bodies, decisions. |
| `events_since` | `since_id?` (default 0), `project?`, `limit?` (default 50, max 500) | Events oldest first and the `since_id` for the next call. |

## Writing

| Tool | Arguments | Effect |
|---|---|---|
| `project_upsert` | `slug`, `name?`, `repo_path?`, `description?`, `session?`, `session_title?` | Creates the project, or updates the given fields. Slugs: lowercase letters, digits, dashes. |
| `task_create` | `project`, `title`, `stage?` (default `plan`), `waiting_on?`, `next_step?`, `branch?`, `doc_path?`, `session?`, `session_title?` | Creates a task with the next key of the project (`acme-shop-1`, `acme-shop-2`, …). With `session`, the task is bound to it. |
| `task_update` | `task`, `title?`, `waiting_on?`, `next_step?`, `branch?`, `doc_path?`, `stage_note?`, `expected_version?`, `session?`, `session_title?` | Patches fields. With `expected_version`, fails instead of overwriting when the task changed meanwhile. |
| `task_set_stage` | `task`, `stage`, `note?`, `session?`, `session_title?` | Moves the task; `note` is shown next to the stage. Moving to `done` sets the closed time. |
| `task_bind_session` | `task`, `session`, `session_title?` | Shows the session on the card as the one working on the task. The previous binding is closed and kept in the task's history (event `task.session_bound`). |
| `report_add` | `task`, `body`, `session?`, `session_title?` | Appends a markdown report. Its first line is the card summary; a first line `state · size · gist` also sets the state (`inspected`, `changed`, `verified`, `committed`, `pushed`, `blocked`) and size (`S`, `M`, `L`). |
| `decision_add` | `project`, `text`, `task?`, `decided_by?`, `session?`, `session_title?` | Records a decision; it appears in the project snapshot. |

Writes that change a task increment its `version`. Every change appends an event and updates open
boards immediately.

## Resource

`relaybay://project/{slug}` — the same markdown as `project_snapshot`, so a session can attach a
project snapshot as context.

## Example

```text
project_upsert(slug="acme-shop", name="Acme Shop")
task_create(project="acme-shop", title="Checkout page", branch="feat/checkout",
            session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e", session_title="Checkout")
  → Created **acme-shop-1** “Checkout page” · stage plan · version 1.
task_set_stage(task="acme-shop-1", stage="dev", session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e")
report_add(task="acme-shop-1", body="verified · M · checkout works\n\nTests pass.",
           session="local_8f14e45f-ceea-467f-a0b6-1f1a2b3c4d5e")
  → Report #1 added to **acme-shop-1** (verified · M): checkout works
```

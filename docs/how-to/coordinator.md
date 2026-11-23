# Coordinate many projects

The coordinator is a role, not a part of the program: an ordinary Claude Code session that you
open yourself, in the Claude desktop app or in a terminal, next to the project sessions. relaybay
starts no coordinator and wakes none; the coordinator works when you talk to it.

What it does with the same MCP tools as any session:

- Catches up with `portfolio_digest` and `events_since`, reads a project with `project_digest` or
  `project_snapshot` and a task with `task_context`.
- Creates projects and cards with `project_upsert` and `task_create`, and binds a project session to
  a card with `task_bind_session` (or gives you the card to start with
  [Copy the brief](work-a-card.md)). A card it makes for another session takes
  `task_create(bind=false)`, so the coordinator is not its bound session.
- Starts a session for a card with the desktop app's start tool: when the start prompt names the
  card's key (`acme-shop-12`) and no other open card, the new session is bound to it at once and
  the coordinator's own binding on it ends ([Binding at start](#binding-at-start)).
- Archives what is finished: `task_archive` on a done or live card (a live one with an open
  question or approval is refused until it is answered or withdrawn), and
  `project_upsert(slug, archived=true)` for a project nobody works on (it leaves the digests and the
  Inbox; `archived=false` brings it back).
- Reads the chats and e-mails you share with it and updates the cards itself; relaybay takes in no
  messages of its own, and sessions talk to each other in the desktop app, not through relaybay.
- Sorts the backlog (`backlog_list`, `backlog_update`).

Name it in `coordinators` in `config.toml` ([configuration](../reference/configuration.md)) to let it
use the tools kept for coordinators: `rules_update`, `plan_accept`, `task_archive`,
`task_unarchive` and backlog triage ([SPEC: Coordinator](../SPEC.md#spec-coordinator)). Project
sessions follow the working contract in the MCP instructions
([SPEC §3](../SPEC.md#spec-mcp-instructions)). Already keeping this in markdown files?
[Import them](markdown-import-export.md). How a coordinator and its agent sessions share the
work on a card, the spec and its change documents: [Coordinator and agents workflow](coordinator-workflow.md).

## Binding at start

A session started from the coordinator with the desktop app's start tool needs no
`task_bind_session` and no `hook_session`:

1. Its first hook names its desktop session, and relaybay creates that session and links its hooks
   to it, so owner comments reach it from its first turn.
2. The start tool's input is searched for card keys (only texts of the form
   `<project>-<number>` of a project on the board; nothing of the input is stored). Exactly one
   open card: the new session is bound to it and the coordinator's binding ends as handed over.
   No key, two open cards, a card another session holds, or a started session the board already
   knew (one with live hooks met over 10 minutes before the launch, or one another session
   started): nothing is bound, and the `session.launched` event on Activity says why.
   `task_bind_session` still binds by hand.

A card the coordinator is bound to itself is handed over by a launch that names it: the
coordinator then no longer holds it. Create cards meant for other sessions with
`task_create(bind=false)`, and do not name the coordinator's own card in a start prompt unless the
new session should take it over.

Write the start prompt with the one card's key in it, for example
`Work the card acme-shop-12: read task_context first.` Plans stay a decision: accept them with
`plan_accept` or on the board.

## Move a markdown coordinator over

A coordinator that keeps its projects in markdown files (a task registry, briefs, per-task notes,
a spec and a status page per project, memory notes) can move them into relaybay and keep the folder
as an export. The examples use a folder `./notes` with the projects `acme-shop` and `demo-api`; the
layout is described in [Import and export a markdown setup](markdown-import-export.md#layout).

### 1. Dry run

```bash
relaybay import ./notes --retro-project acme-shop
```

Nothing is written. Read the list:

- `refused` items: a secret in a file (move it out, then run again), a name that is no valid slug,
  a file that is too large.
- `conflict` items: the board already has something under that name. Decide per item: export first
  (the board wins) or `--overwrite` (the file wins).
- Warnings about stages: a stage word relaybay does not know. Map your own column, stage and heading
  names in an aliases file outside any repository and pass it with `--aliases`:

```json
{
  "columns": {"Aufgabe": "task", "Phase": "stage", "Wartet auf": "waiting_on"},
  "stages": {"Entwicklung": "dev", "Abnahme": "test"},
  "headings": {"Entscheidungen": "decisions"}
}
```

Run the dry run again until the list says what you expect.

### 2. Apply

```bash
relaybay import ./notes --aliases ./aliases.json --retro-project acme-shop --apply --expect <hash>
```

`<hash>` is the plan hash the last dry run printed: if the files or the board changed since, nothing
is written. Then open the board: the tasks with their stages, and **Docs & rules** of a project or two
(the brief, the spec, the status page, `task-<ref>` documents linked from their tasks, decisions and
notes).

### 3. Link what stays in a repository

A document that must stay next to the code (a runbook, a README, an ADR folder) is linked, not
copied:

```text
doc_update(project="acme-shop", doc="deploy", body="-", reason="Kept next to the code",
           base_version=0, kind="runbook",
           source_url="https://git.example.com/acme-shop/blob/main/docs/deploy.md")
```

The board shows where it lives and does not edit it; import and export leave it alone. If a copy was
already imported, link it with one more `doc_update` that passes `source_url` and the current
`base_version`; later writes then need `force=true`.

### 4. Shrink the coordinator's instructions

Its own instructions no longer describe files. What is left is roughly:

- Start with `portfolio_digest`; drill into a project with `project_digest` and into a task with
  `task_context`. The context comes from the
  [digests](../SPEC.md#spec-digest), so the files need not be read.
- Read a document with `doc_get` (one section with `section`), change it with `doc_update` and the
  `base_version` you read; on a conflict merge the current text it returns.
- Record decisions with `decision_add`, preferences and facts with `memory_add`.
- Point a task at its notes with `doc_path="relaybay://doc/<project>/task-<ref>"`, and link sections from
  descriptions and reports as `relaybay://doc/<project>/<doc>#<anchor>`: the board turns them into links.
- Keep findings for later on the backlog, not in your own files: sessions and reviewers call
  `backlog_add`; review the queue with `backlog_list`, sort it with `backlog_update(status="later")` or
  `status="declined"` with a reason, order it with `priority` and name who it is for with `assignee`
  (configured coordinators only); `backlog_list(assignee="-")` shows what nobody has yet. The owner turns an item into a
  task with **To plan** on the board; `planned` is never set over MCP. A `backlog.md` in your notes
  imports and exports like the task registry.

Project sessions need no change beyond what the MCP instructions already tell them.

### 5. Export as the target

```bash
relaybay export ./notes --aliases ./aliases.json            # what would change
relaybay export ./notes --aliases ./aliases.json --apply    # write it
```

From now on the board is the source of truth and the folder is its export, for git history, backup
and grep. Your own files stay as they are until a board change makes them differ; then `--force` once
takes each over. Schedule the export daily if you want ([On a schedule](markdown-import-export.md#on-a-schedule))
and commit the folder afterwards.

# Coordinate many projects

!!! note
    A full guide is planned. In short: one session acts as the coordinator. It creates
    projects and tasks with `project_upsert` and `task_create`, binds project sessions with
    `task_bind_session`, catches up with `events_since`, and reads each project through
    `project_snapshot`. Project sessions follow the working contract in the MCP instructions
    ([SPEC §3](../SPEC.md#spec-mcp-instructions)). Already keeping this in markdown files?
    [Import them](markdown-import-export.md).

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

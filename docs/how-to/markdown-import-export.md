# Import and export a markdown setup

If your projects already live in markdown files (briefs, a task registry, task notes, memory notes),
`relaybay import` brings them onto the board and `relaybay export` writes the board back to the same
folder layout for git history, backup and grep. The rules behind this page:
[SPEC §8](../SPEC.md#spec-import).

## Layout

```text
<root>/
  TASKS.md                             task registry: a markdown table, one row per task
  projects/<slug>.md                   project brief
  projects/<slug>/docs/<doc>.md        further documents (spec, status, runbook ...)
  projects/<slug>/tasks/<ref>/TASK.md  per-task notes
  projects/<slug>/memory/<name>.md     notes of one project
  projects/<slug>/decisions.md         written by export, never read by import
  memory/<name>.md                     global notes, with frontmatter
  retro.md                             retro registry
  backlog.md                           backlog: a markdown table, one row per item
```

Other markdown files under the root's top level, `projects/` and `memory/` are listed as `skipped`.
`memory/MEMORY.md` counts as an index of the folder and is skipped too. Other folders are not looked
at.

## What goes where

| File | Becomes |
|---|---|
| `projects/<slug>.md` | The project (created if missing, named after the first `# Heading`) and its document `brief`: the whole file, contacts included. Bullets under `## Decisions` become project decisions with the source `message:import <path>`. |
| `projects/<slug>/docs/<doc>.md` | Document `<doc>`. The kind comes from the name: `spec`, `status`, `task`, `brief`, `runbook`, `adr`, `retro`, anything else `doc`. |
| `TASKS.md` | Tasks. Columns: project, task (the title), stage, waiting on, next step, document, and an optional ref. |
| `projects/<slug>/tasks/<ref>/TASK.md` | The matching task's description (the `## Task` section, else the first paragraph), its `## Decisions` bullets as task decisions, and the whole file as document `task-<ref>`, linked through the task's document field as `relaybay://doc/<slug>/task-<ref>`. A folder with no registry row creates the task in stage plan. |
| `memory/<name>.md` | A global note: title from `name` or the first heading, kind from `metadata.type` (`user`, `feedback`, `project`, `reference`; anything else is `reference`), the `description` as the first line of the body if it is not already, `[[links]]` kept in the note's links. `metadata.role` makes it a role note. |
| `projects/<slug>/memory/<name>.md` | The same, as a note of that project. |
| `retro.md` | Document `retro` of the project named by `--retro-project`; skipped with a warning without it. |
| `backlog.md` | Backlog items. Columns: date (`2026-11-05`), project, area, what (the title), origin (`session`, `review`, `intake`, `owner`; anything else is `owner`) and decision (`planned`, `later`, `declined: <reason>`, empty for new). An item is the same item again when project, date and title slug match. `planned` only records history: import creates no task. An unknown project is an error line. Other column names map through `--aliases` under `"backlog"`. Export writes every item, oldest first. |

**Which row is which task folder.** A row finds its folder through its `ref` cell, then a `document`
cell holding a path through `tasks/<ref>/` or a `relaybay://` link, then the title as a slug
(`Gift cards` finds `tasks/gift-cards/`).

**Stages.** The leading word of the stage cell goes through the alias map and the rest becomes the
stage note: `in progress - refunds still open` is stage `dev` with the note `refunds still open`. An
unknown word gives stage `plan`, the whole text as the note, and a warning.

**Other names, any language.** English names are built in. `--aliases` adds yours from a JSON file,
which you keep outside any repository if it says something private:

```json
{
  "columns": {"Aufgabe": "task", "Phase": "stage"},
  "stages": {"Entwicklung": "dev"},
  "headings": {"Entscheidungen": "decisions", "Aufgabe": "task"}
}
```

Columns map to `project`, `ref`, `task`, `stage`, `waiting_on`, `next_step`, `document`; stages to the
six stage keys; headings to `decisions` or `task`.

## Dry run, then apply

Nothing is written without `--apply`. The dry run works on a throwaway copy of the database, so it
neither migrates nor changes the real one (SQLite may leave its empty `-shm` and `-wal` files next to
it), and it can run next to `relaybay serve`.

```bash
relaybay import ./notes --aliases ./aliases.json --retro-project acme-shop
```

```text
relaybay import: /home/demo/notes (dry run)
                  add    update unchanged   skipped  conflict   refused     error
  project           2         0         0         0         0         0         0
  document          9         0         0         0         0         0         0
  task              7         0         0         0         0         0         0
  ...

  add       document acme-shop/spec (projects/acme-shop/docs/spec.md)
  ...

Plan hash: 351a2fbefecc93d5 (write exactly this plan: add --apply --expect 351a2fbefecc93d5 to this command)
Nothing written.
```

A changed document or note shows a unified diff (at most 40 lines each, 400 in all). Then write
exactly that plan; if the files or the board changed in between, the hash no longer matches and
nothing is written:

```bash
relaybay import ./notes --aliases ./aliases.json --retro-project acme-shop --apply --expect 351a2fbefecc93d5
```

`--apply` without `--expect` writes whatever the plan is at that moment. All writes go through the
same service as the board and MCP (events, search index, validation), one item at a time, so an
error in one item does not stop the others. A running server shows the imported items after a page
refresh. The exit code is `0`, or `1` when anything was refused, in conflict or failed.

## Running it again

A second run with no changes writes nothing and adds no events: `import_item` remembers what each
item was. A changed file updates through the service: a new document version with the reason
`import`, a note update, task fields only where the file has a value (an emptied cell leaves the
field alone).

A note's kind and role stay as they were created. Removing a row or a file removes nothing.

A document that lives in a repository (it has a `source_url` on the board) is not taken from the
folder: a changed file of it is `skipped`, also with `--overwrite`. Change it in its repository.

## Conflicts

The board is never overwritten silently. When a target changed since its last import, or exists on
the board without having come from an import, the item is listed as `conflict` and skipped, unless
the file already says the same. You have two ways out:

- `relaybay export` brings the file up to date with the board, then the import sees no difference.
  A file export did not write takes `--force`.
- `--overwrite` takes the file's text. For a document that is a new version; the old ones stay.

A task's description is the one exception: export cannot write it back into `TASK.md`, so a board
edit alone is no conflict, only a `TASK.md` whose description also changed.

**Decisions are append-only.** A new bullet adds a decision. An edited bullet adds a new decision
that supersedes the old one (the texts must still share 60%; otherwise it counts as new). A bullet
that left the file leaves its decision current, and a decision superseded on the board is not added
again; both are listed as `skipped`.

## What is refused

- **Secrets.** An item with a secret value is `refused`, naming the file, the line and the detector,
  never the value; the rest goes on. A registry row is named by its line (`TASKS.md:7`), and a file
  name holding a secret by a hidden name. The detectors and allowances are those of the
  [policy](../SPEC.md#spec-policy).
- **Paths.** Only files under the root are read. A link leaving the root is refused. Project,
  document and task folder names must be valid slugs, so no name can lead anywhere else.
- **Size.** A file over 2 MB, a document over 200000 characters, a note over 8000.
- **Encoding.** A file that is not UTF-8 is an error for that file alone.

## Export

```bash
relaybay export ./notes            # what would change
relaybay export ./notes --apply    # write it
```

Export writes documents (a document that lives in a repository only when it has text of its own),
current and superseded decisions (`projects/<slug>/decisions.md`), current
notes and the task registry to the same layout. A document goes back to the file it was imported from.
It reads a copy of the database, so it changes nothing on the board. The output is deterministic
(sorted, no times but the stored ones), so git diffs stay small, and an import of an export changes
nothing.

The registry gets a `ref` column, so a task keeps its identity when it is renamed on the board.

The manifest `.relaybay-export.json` records what export wrote, with hashes:

- A file export did not write is never overwritten without `--force`, whatever it holds: it is a
  `conflict`, and `--force` takes it over. Only after that does export keep it up to date.
- A file export wrote and you changed by hand is a `conflict` too, until `--force`. It stays protected
  also while it says the same as the board in other words.
- A file that says the same in its own words stays as it is: exporting into the folder you imported
  from leaves your hand-written notes and registry alone until the board really differs. Pass the
  same `--aliases`, so export can read your registry.
- A file export wrote and the board no longer has (a superseded note) is removed, unless it was changed
  by hand. Files it did not write are never removed, and neither is anything outside the layout, even
  if the manifest names it.
- Two items for one path never overwrite each other: a later note gets a `-<id>` suffix, a later
  document moves under `docs/`; with no place left it is an `error`.
- Export refuses to run without a database, and to `--apply` from a board with no projects into a
  folder that holds an earlier export: both look like a wrong `--home`.

In the diff, a file on disk holding a secret shows only `diff hidden: line N (detector)`.

Not exported: reports, comments, plans, blocks, environments and a task's description, progress and
branch. Secrets are not stored, so there is nothing to redact. Import does not read `decisions.md`; a
decision comes in through the `## Decisions` of a brief or `TASK.md`.

### On a schedule

A daily export is a job for your scheduler. With cron, every day at 06:00:

```text
0 6 * * * relaybay export /home/demo/notes --apply
```

With Task Scheduler on Windows:

```powershell
schtasks /Create /TN "relaybay export" /SC DAILY /ST 06:00 /TR "relaybay export C:\notes --apply"
```

Commit the folder afterwards if you want the history in git.

## Moving a coordinator over

The full walk-through: [Coordinate many projects](coordinator.md#move-a-markdown-coordinator-over).

1. Dry run, fix what is refused, map your column and stage names with `--aliases`.
2. Apply. Check the board and **Docs & rules** of a project or two.
3. Export into the same folder. From now on the folder is the export target and the board is the source
   of truth. Your own files stay yours until a board change makes them differ: then `--force` once
   takes each over.
4. Shrink the coordinator's own instructions to "start with `portfolio_digest`": its context comes
   from the [digests](../SPEC.md#spec-digest), so it no longer needs to read the files.

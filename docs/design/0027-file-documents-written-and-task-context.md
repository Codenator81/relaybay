# 0027 — File documents written through relaybay, and a task context that fits the task

- Status: **Accepted** 2026-11-19, for 0.15. Builds on [0026](0026-project-knowledge.md) and
  supersedes its decision K2 ("relaybay never writes a file document") and the "Never written by
  relaybay" item of its File documents section.
- Two parts. **A**: `doc_update` writes a file document: the change goes into the file on disk,
  only in the lines it replaces, and a change made to the file outside relaybay is seen and named.
  **B**: `task_context` carries what the task needs and not what another task decided: the
  document sections linked to the card, the open backlog items of the same subject, and only the
  decisions that belong to it.
- Size: about 3 agent-days in four pull requests and a release. Two migrations (0054, 0055), both
  additive. MCP contract stays **version 2**: one new tool and new optional parameters only.

## Why

The owner's word (2026-11-19): relaybay is the truth for a project's knowledge, and the files keep
existing for the repositories and the sessions that read them. Measured on copies of a real
project:

- A spec of 866 KB lives in a repository and is read by sessions as a file; with 0.14 it can be
  read through relaybay by section, but every change still goes around relaybay. Two ways to change
  the same text is how they drift.
- The task context of a payout-provider card listed two project decisions about a registration
  form and the release flow: the only filter was "project scope, newest first". Six current
  decisions exist in that project, two of them project-wide, none of them about payouts.
- The card's sections exist only as links written into the description or progress text; a
  coordinator who sets up a card has no tool to attach a section, and nothing else of the card
  (its subject, its open findings) follows from the sections.
- 53 open backlog items in the project, none of them shown to the session working on a card of
  the same subject.

## Part A: writing a file document

### The rule

`doc_update` on a file document is no longer refused. With `section` it replaces that section,
with `lines` + `expected` those lines, without either the whole text; the same parameters as a
database document. The text goes into the file:

1. Read the file's bytes and hash them. When the hash is not the one relaybay last saw, the file
   was changed outside relaybay: the read path's refresh runs first (index, event, the outside
   mark below) and the version moves on.
2. `base_version` is checked as for a database document. A file document's `version` counts the
   hashes it has had: it moves on with every write through relaybay and with every outside change
   that a read notices. A write with an older `base_version` is a conflict whose message holds the
   current section (or text). `lines` + `expected` writes whatever the version, as today.
3. The change is spliced into the bytes: only the replaced lines change. The file's own line
   ending (CRLF or LF; for a file with mixed endings, the ending of the line the change starts at),
   its byte order mark and the newlines at its end are kept. The new text is checked for secrets
   as any `doc_update` body.
4. The new bytes go to a temporary file in the same folder, which replaces the file
   (`os.replace`). Right before the replace the file is hashed again; if it changed in between,
   nothing is written and the answer is the conflict.
5. The new hash, size and time are stored, the index is rebuilt and one `document.updated` event
   names the changed sections, `by relaybay`.

So a file in a git working tree shows, after a section write, a diff of that section only. The
write does not commit; the repository's owner does, as for any edit in the working tree.

Limits: the file's size limit (8 MB) applies to the result; the path rules (`attachment_roots`,
the policy's denied paths, relaybay's data folder) are checked again before each write; a file
that is missing or unreadable refuses the write and names why.

### A change made outside relaybay

The text in the file can still be edited by an editor, a script or a session's own file tools.
That is not prevented; it is made visible:

- Every read compares the file with what relaybay last saw (0026). A different hash that relaybay
  did not write is **an outside change**: the `document.updated` event carries `outside` and the
  changed anchors, and the document records when and which sections (migration 0054:
  `file_changed_by` `relaybay` or `outside`, `file_changed_at`, `file_changed_sections`).
- While the last change is an outside one, `doc_outline`, `doc_get`, `doc_search` hits of that
  document and the linked-section lines of `task_context` add one line:
  `Note: <doc> was changed outside relaybay at <time>: sections a, b (version N).` The next write
  through relaybay clears it.
- A write never overwrites an outside change silently: step 1 turns it into a new version first,
  so a writer holding the old `base_version` gets the conflict with the current text.

Chosen over an import with a conflict state because the file is already the merged result
(whoever edited it saw the whole text); a conflict object would need a merge tool relaybay does
not have. Chosen over a warning only on the board because the sessions are the ones that write.

### Not in this change

- The board's document page stays read-only for file documents (it shows the path and the lines);
  editing a section on the page is a later change.
- No watcher: an outside change is seen at the next read or write, as in 0026.

## Part B: a task context that fits the task

### Linked sections

A new tool **`task_doc_link(task, doc, section, remove?)`** links a section of a project document
to a card (migration 0055: `task_doc_section` with the task, the document, the anchor, who and
when). The coordinator links the sections when it sets the card up; a session may link what it
finds. An explicit anchor or a heading anchor is accepted; the anchor is checked against the
document's current outline, and a link whose section later disappears stays listed as broken.

- `task_context` lists the linked sections first in "Linked document sections" (before the links
  found in the card's text, the limit of 5 lines stays, the rest is counted), in the 0026 pointer
  form: anchor, title, lines, size, and the `doc_get` call.
- `task_get` and the card page list them; `task_create` gets an optional `sections` (a list of
  `doc#anchor`).

### The card's subject

What the card is about, for the two filters below, is a set of word stems (the stemmer of
`memory_search`, words of four letters and more, without common words) taken from:

1. the card's area name, when it has one;
2. the titles and heading paths of its linked sections;
3. the card's title.

### Open backlog items of the same subject

`task_context` adds **"Open backlog items"**: items with status `new`, `later` or `planned` of the
card's project that

- are linked to the card (`task`), or
- have an area whose stems meet the card's subject, or
- point inside a linked section (a `location` or a `path:line` in the text that falls in the
  section's lines of the same file, or the section's anchor or number in the text).

One line each, `#id [area] title · kind · priority`, linked ones first, then by priority. At most
8, the rest counted with the `backlog_list` call.

### Decisions that belong to the card

"Decisions" lists the card's own decisions first, then project decisions **related** to the card:

- the decision's `area` (new, optional on `decision_add`) is the card's area, or
- its text names a linked section (its anchor, or its number such as `9.6a`), or
- its text holds at least two stems of the card's subject.

Unrelated project decisions are not listed; one line counts them with the `decision_list` call.
A project with project-wide rules that every card must see keeps them in the project rules
(`rules_get`), which `task_context` already names.

### Budget

The new parts are cut first when the budget is short, in this order: backlog items, then related
project decisions, then linked sections beyond the first. The total never exceeds the budget.

## Data

| Migration | What |
|---|---|
| 0054 | `document.file_changed_by`, `file_changed_at`, `file_changed_sections`; nullable |
| 0055 | `task_doc_section` (task, document, anchor, created_by, created_at, unique per task and anchor); `decision.area` nullable |

## MCP contract

Version 2, additions only: the tool `task_doc_link`; `doc_update` accepts a file document;
`decision_add(area)`; `task_create(sections)`. The `doc_update` description stops saying that a
file is never written.

## Import of a bug registry into a project that already has items

`relaybay import --backlog <file>` gets **`--project <slug>`**: only the rows of that project are
read, the others are listed as skipped. A row that matches an open item already on the board is
not created again; it is recorded as imported into that item (so a second import is a no-op). A
row matches an item when

- its text names the item (`backlog #N` or `#N` of the same project), or
- its title is similar to the item's title (the title similarity of `backlog_add`, 0.6 and up), or
- the item's title words are nearly all in the row's text (stems, 0.7 and up).

The dry run lists every match with its reason and every near miss (0.4 to the threshold) as a
created item with a warning, so a person checks those before `--apply`.

## Plan by pull request

| PR | What |
|---|---|
| 1 | this design, ROADMAP, designs index |
| 2 | Part A: `doc_update` writes a file document, outside changes (0054) |
| 3 | Part B: `task_doc_link`, the card's subject, backlog items and related decisions in `task_context` (0055) |
| 4 | registry import with `--project` and matches against existing items |
| 5 | release 0.15.0 |

## Risks

- **relaybay writes into a working tree it does not own.** Only the replaced lines change, the
  write is atomic and checked twice by hash, and the path rules apply; the commit stays with the
  repository's owner. A session that has the file open in an editor sees it change on disk.
- **A subject by words finds too much or too little.** The lists are capped and name the call that
  shows the rest; a card with linked sections and an area is matched by those first.
- **Hidden project decisions.** A project-wide decision with no area and no shared words is no
  longer in every card's context; the count line names `decision_list`, and rules are the place
  for what every card must follow.

## Decided points

| # | Point | Default | Other way |
|---|---|---|---|
| F1 | A file document written through relaybay | yes, section, lines or whole text, spliced in place | read-only (0026 K2) |
| F2 | An edit of the file outside relaybay | a new version with an "outside" note until the next write | an import with a conflict to resolve |
| F3 | The board's page of a file document | read-only | edit a section on the page |
| F4 | Project decisions in `task_context` | only related ones, the rest counted | all, newest first |
| F5 | A registry row that resembles an existing item | matched at 0.6 title similarity or 0.7 word cover, near misses warned | always a new item |

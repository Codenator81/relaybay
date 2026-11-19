# 0026 — Project knowledge and a quieter board

- Status: **Accepted**, 2026-11-19, for 0.14. Builds on [0008](0008-memory-and-context.md) and
  [0011](0011-docs-in-database.md) (documents, search, import), [0014](0014-task-plan.md) (the
  plan reminder), [0015](0015-intake-and-autonomy.md) (the backlog) and
  [0025](0025-board-with-the-desktop.md) (the board next to the desktop app). Nothing that 0.12
  removed comes back: no intake, no messages between sessions, no schedules, no usage, no brake.
- Two parts. **A**: a project's knowledge (a large spec, a status, a bug registry, the task
  registry) can be read and written through relaybay without loading a whole document into a
  context. **B**: the Inbox, the digests and the hooks say only what needs someone.
- Size: about 6.5 agent-days in six pull requests and a release
  ([plan](#plan-by-pull-request)). Three migrations (0051 to 0053), all additive. MCP contract
  stays **version 2**: new tools and new optional parameters only.
- Real data of a working project is not migrated by this work. Every number below was measured on
  copies: a 866 KB spec of a real project, a 33 KB status file, a bug registry of 122 rows, a task
  registry of 45 rows and a copy of the maintainer's own board database (6 projects, 90 cards).

## Why

The board holds tasks well and knowledge badly. Measured on the copies:

- **The spec cannot be stored at all.** It has 847,944 characters; a document may have 200,000
  (`DOC_BODY_MAX`). The secret scan refuses it as well: 13 lines of prose about tokens and
  `Authorization` headers match two detectors, none of them a secret.
- **If it could be stored, one read would not fit a context.** `doc_get` returns the whole text:
  869 KB, about 248,000 tokens. One section by its anchor is no answer either: the largest
  second-level section is 210 KB (about 60,000 tokens), the largest third-level one 52 KB (about
  15,000 tokens). There is no outline; the only list of anchors is the error text of a wrong
  anchor.
- **Search finds the document, not the place.** `memory_search` returns one hit per document with
  one line number. Three test queries had 7, 5 and 19 lines holding every word; each returned a
  single line, two of them inside the table of contents.
- **A one-line change sends 52 KB and stores 0.87 MB.** A section is the smallest unit of a write,
  every version keeps the whole text, and the database grew by about 2.2 MB per version (text plus
  index). That spec changed in 15 commits in 30 days.
- **The live board has no documents at all**: 0 documents, 0 memory notes, 40 cards whose document
  field is a file path. The knowledge stayed in files because of the points above.
- **The Inbox says 99; 24 cards wait for the owner.** The other 75 rows are done cards, ended
  sessions, a demo project and the same card listed up to four times
  ([table](#the-inbox-99-rows-24-cards)).

## Part A: project knowledge

### What exists and what is missing

| Need | Today | Missing |
|---|---|---|
| Outline of a document | section anchors from ATX headings; the document page lists them | an MCP outline with sizes; explicit anchors |
| Read one section | `doc_get(section)` | a size cap; a part of a section; a pointer instead of text |
| Search with a pointer to the place | `memory_search`: one hit per document, one line | hits per section with the anchor and the lines |
| Card to spec section | `relaybay://doc/<project>/<doc>#<anchor>` in the description, progress or document field; `task_context` shows the heading and 3 lines, at most 5 links | the section's size, its lines, the file, the call that reads it |
| Edit one section | `doc_update(section)` with `base_version`, conflict text, history, diff | byte-exact replacement (4 of 84 identity replacements changed a blank line); a unit below a section |
| Import and export | `relaybay import` / `export`, idempotent, dry run with a plan hash | the file's own bytes: line endings and the final newline are normalised on import |
| A document that lives in a repository | `source_url` (an http link): the board links out, relaybay has no text | reading, outline and search of the file itself |
| Bug registry | the backlog: status, area, origin, priority, assignee, a task from "To plan", `backlog.md` import | kind, code location, source text, link to an existing task, duplicates, search |
| Human reading | document page with heading anchors, section list, history, diff; a search box on Docs & rules | a table of contents that stays in view; search inside a document |
| One line per open task for a coordinator | `task_list()` over all projects (32 open cards in 11,082 characters), the registry written by `relaybay export` | the same list inside `portfolio_digest` |

### Source of truth

**Decision: a document that is edited together with code lives in its repository as a file, and
relaybay indexes that file. A document without a repository home (status, brief, runbook, a task's
notes) lives in the database, as today.** relaybay never keeps a second editable copy of a file.

Why, for the measured spec:

- It is changed in the same commit as code (15 commits in 30 days, +5,336 and -557 lines, one of
  them with 81 files). A branch has its own version of the spec until it is merged, and the review
  of a spec change is the pull request's diff. A database has one head and no branches.
- Its references are code: 614 backticked `Class.Method` symbols and 60 file paths (none with a
  line number), 288 section signs, and 64 of its 68 in-document links already equal the anchors
  relaybay derives from headings. They stay valid only next to the code they name.
- History, authorship and conflicts are what git already does for it. Full-text versions in the
  database would add about 2.2 MB per change to a 31 MB file.
- A file is byte-identical to itself. Any import and export pair has to prove that; an index has
  nothing to prove.

The other way, and why not: **the database as the source with a sync to the file.** It needs a
merge in both directions (an agent edits the section over MCP while a developer edits the file in
a branch), it writes into working trees it does not own, it has to choose one branch as the truth,
and every failure of the sync leaves two texts that both look current. The existing risk line of
0011 ("two sources of truth during the move") would become permanent.

### File documents

A document may point at a file: one of the project's repositories (`project_repo`, by name) and a
path relative to its root.

- **Created** with `doc_update(base_version=0, source_repo=..., source_path=...)` and body `-`, or
  with **New document → A file in a repository** on Docs & rules. The path is resolved under the
  repository root; a path that leaves it, also through a link, is refused (the check
  `escapes_root` of the import). A file over 8 MB or not UTF-8 is refused.
- **Read on demand.** Each read compares the file's size and modification time with the stored
  ones, hashes it when they differ and rebuilds the section list and the section index when the
  hash differs. Nothing watches the disk. A changed hash writes one `document.updated` event with
  the anchors of the sections that were added, removed or changed, so Activity and the digests say
  "spec changed: 3 sections" without a model.
- **The text is not versioned in the database.** `doc_history` of a file document lists those
  events (when, which sections) and says the history is in git.
- **Never written by relaybay.** `doc_update` on a file document is refused, as it is today for a
  document with `source_url`, and the refusal names the file and the section's lines: the agent
  edits the file with its own tools, in its own working tree, and commits it with the code. The
  board shows "lives in `<repo>/<path>`", no edit links, and the existing "open the code" action.
- **Which checkout.** The file is read from the repository path the project names. A session in a
  worktree gets the repository-relative path and the anchor, so it reads its own copy; the header
  of every read names the branch and the short content hash it came from.
- **Not reachable** (a server in a container, another machine): the document stays listed with
  "file not found at ...", and the tools return the pointer only.
- **Secrets.** The index is the only place where the file's text is stored. A line that matches a
  secret detector of the project's policy is indexed with the value masked, and the outline says
  how many lines were masked; the document is not refused. Reads pass the file's text through.
- `source_url` documents stay as they are.

### Reading a large document

For file documents and database documents alike.

1. **Explicit anchors.** `<a id="name"></a>` on its own line directly above a heading (blank lines
   allowed) is an alias of that section: `doc_get`, links and the page accept either. A heading
   slug changes when the heading is reworded; an explicit anchor does not, so a card's link
   survives. relaybay's own SPEC has 168 such anchors.
2. **`doc_outline(project, doc, depth?, under?)`**: one line per heading with level, anchor (and
   alias), title, lines `a-b`, characters and the token estimate (bytes / 3.5). `under` shows the
   headings inside one section. For the measured spec (84 headings) the whole outline is about
   7,800 characters.
3. **`doc_get` gets `lines` and `max_chars`.** `lines="a-b"` reads a range, which is the unit
   below a section: 24 of the 84 sections are over 12,000 characters, 2 over 40,000. A read over
   the cap returns the section's first lines, the outline of what is inside it and the call that
   reads on (`doc_get(..., lines="...")`), never a silent cut. The default cap applies only to a
   document over 200,000 characters (none could exist before): 40,000 characters, about 10,000
   tokens. A smaller document reads as before unless `max_chars` is passed. `doc_get` of such a
   large document without a section returns its head and the outline.
4. **`doc_search(project, query, doc?, limit?)`**: hits per section: document, anchor, the heading
   path, the line, the matching line with one line around it, the section's size. A second FTS5
   table, `doc_section_index` (one row per section with its own text up to the next heading),
   created by the service like `search_index` and rebuilt per document when its text or hash
   changes; the `LIKE` fallback covers it too. `memory_search` keeps returning one hit per
   document and adds the anchor of the section that holds its line.
5. **The card's link carries the size.** The "Linked document sections" lines of `task_context`
   and of the session-start context become
   `spec#gift-cards "Gift cards" · lines 2140-2610 · 52k chars (~15k tokens) · shop:docs/spec.md · doc_get(project, doc, section="gift-cards")`
   and no document text beyond the heading. A link without an anchor names the document's size
   and `doc_outline`. The limit of 5 links and the `broken` mark stay.

### Writing

- **File documents**: the agent edits the file (above). One section, a conflict and the history
  are the editor's and git's.
- **Database documents** keep `doc_update` with `base_version`. Two defects are fixed:
  `replace_section` changes only the lines of the section it replaces (today it trims and adds
  blank lines at the section's end, which showed as 3 changed lines for a one-line edit and as 4
  failures in 84 identity replacements), and `doc_update` gets `lines` + `expected` for a change
  below a section: the lines `a-b` are replaced when their current text equals `expected`,
  otherwise the answer is the conflict with the current lines.
- **Byte-identical import and export.** Stored text stays normalised (LF, no outer blank lines),
  so comparisons and diffs keep working. `import_item` records what the normalisation removed:
  the line ending (LF or CRLF), a byte order mark, the blank lines before the text and the newlines
  after it. `relaybay export` puts them back, so a file that was imported and not changed on the
  board is written back with the same SHA-256. A file with mixed line endings is imported with a
  warning line that says its export will use LF. Measured: the status file has 91 CRLF lines, the
  spec loses one final newline; both would differ after today's round trip.
- The limit of a database document stays 200,000 characters. A larger one belongs in a repository.

### The backlog as a bug registry

The bug registry measured: 122 rows in 6 columns (date, project, area, what, source, decision), 3
projects, 118 different area texts, 36 rows with a `file:line` in the text, a "what" of 202
characters at the median and 667 at most, dates in two formats (61 rows `dd.mm` without a year),
a free-text decision in about half the rows. The board's backlog: 95 items, 91 of them `new`.

The backlog is the right object: it already has status, origin with the task and the session,
priority, assignee and the task that "To plan" made. Added (migration 0051, all nullable):

| Field | What | Why |
|---|---|---|
| `kind` | `bug`, `idea`, `question`, `debt`; empty for existing items | a registry mixes them; the filter that matters most |
| `location` | where in the code: `repo:path:line` or a link, up to 300 | 36 of 122 rows carry one inside the text |
| `origin_text` | the source in words, up to 200 | today a source that is not one of four words imports as `owner` and is lost |
| `triage_note` | the decision in words, up to 500 | a decision outside the five statuses is an import error today |
| `duplicate_of` | another item | closes a duplicate without losing where it came from |
| `found_on` | the date the finding was made | an imported row is older than its import |

- `backlog_add` gets `kind` and `location`. It always writes, as today; when open items of the
  project have a similar title (the title similarity of `memory_add`, 0.6 and up) the answer adds
  `Note: similar open items: #12 (0.71) ...; if it is the same, backlog_update(item, duplicate_of=12)`.
- `backlog_update` and `backlog_patch` get `kind`, `location`, `triage_note`, `duplicate_of`
  (status `declined`, reason "duplicate of #N") and `task`: a coordinator links an **existing**
  card, and the item becomes `planned`. "To plan" on the board still creates a new card.
- `backlog_list` gets `query`, `kind`, `since`, `task` and `has_location`. Backlog items join the
  search index as kind `backlog`. The Backlog page gets a search box, a kind filter and the new
  fields on the card.
- **Import of a registry table.** `relaybay import` already reads `backlog.md` with column aliases
  in any language. It gets `--backlog <file>` for a registry under another name, and stops
  refusing real rows: a "what" over 200 characters becomes a title cut at a word within 120
  characters plus the whole cell as the body; a date without a year takes the latest such date
  not after the file's modification time; a project is matched by slug or by name; a decision
  that is not in the vocabulary (or the aliases) leaves the item `new` with the text in
  `triage_note` and a warning, not an error; the source goes to `origin_text`; the first
  `path:line` of the cell goes to `location`. Export writes the same columns back.

### Human reading

The document page already renders the measured spec in 0.28 s (986 KB of HTML). Added:

- the section list becomes a table of contents that stays in view, folded to two levels, with the
  current section marked and each section's size;
- a search box on the page: `doc_search` in this document, hits grouped by section, a click goes
  to the anchor with the words marked;
- for a file document: the repository and path, the branch and when the file last changed, no
  edit links;
- a search hit on Docs & rules links to the section, not to the top of the document.

### What a coordinator needs instead of a hand-kept task registry

The hand-kept registry has one row per open task: project, task, stage with where the change is
deployed, who waits and for what, the next step, the task's document. relaybay has each of these
(stage and `stage_note`, `waiting_on`, `next_step`, branch, pull requests, the document field), and
`task_list()` prints them for all projects. What stops a coordinator from dropping the file:

- `portfolio_digest` has no line per open task, and at the default budget its "Waiting for the
  owner" section takes 11,481 of 12,000 characters (97 lines). After Part B that section has
  about 28 lines, and the digest gets a section **Open tasks**: one line per open card that is not
  already listed above, by project, work in progress first:
  `[test] demo-api-7 Retry webhooks · note: on the test stand since 12.11 · waiting on: QA · next: ... · PR #41 · doc`.
- `task_list` lines add the pull requests and the document.
- Where a change is deployed stays free text in `stage_note`; the MCP instructions ask for it at
  each stage move. No new field.
- A file copy for people is `relaybay export` (it writes the registry).

## Part B: noise

### The Inbox: 99 rows, 24 cards

`Inbox.count` on the copy, by the list each row comes from:

| Rows | What | Noise or signal | Fix | After |
|---|---|---|---|---|
| 25 | cards waiting on the owner | signal: 24; 1 is the demo project | an archived project leaves the Inbox | 24 |
| 37 | "work log not entered" on done cards: 29 in a project where no card ever had a tracker reference, 8 in a project with a tracker | 29 noise; 8 a reminder, not 8 decisions | no requirement in a project without a tracker; the rest as one line "8 done cards without the work log" linking to the list | 1 |
| 17 | unlinked hook sessions: 15 ended (2 to 16 days ago), 2 idle | 15 noise | ended sessions are not listed; the others as one line linking to Sessions | 1 |
| 10 | rule warnings: 8 on done cards ("done usually needs the progress field"), 2 on cards in test | 8 noise; 2 are cards already listed | a done card's warning stays on the card only; an open card's is a mark on its row | 0 |
| 3 | "to accept" (cards in review or test with a checklist) | all 3 already listed as waiting | a mark on the card's row | 0 |
| 3 | "agent reply (1 unread)": 2 on cards closed 13 and 16 days ago, 1 on an open card | 2 noise; 1 already listed | closing a card marks its agent replies read; an open card's is a mark on its row | 0 |
| 2 | owner comments not delivered for over an hour | both cards already listed | a mark on the card's row, with the reason | 0 |
| 1 | "91 new backlog items" | signal, one line | unchanged | 1 |
| 1 | worktrees left by the runs of 0.11 | one-time, until Seen | unchanged | 1 |
| **99** | | | | **28** rows: **24** cards and 4 lines |

Rules behind the table:

1. **One row per card.** A card appears once, with every reason as a mark: waits for you, to
   accept (n of m), reply unread, comment not delivered (why), rule warning. Two cards on the copy
   were listed four times each.
2. **A done card is not in the Inbox.** Its warning and its replies stay on the card.
3. **Grouped lines are one row each** and link to a filtered list: work log, unlinked sessions,
   backlog.
4. **The badge and the digest count cards**, 24 here; the lines are listed under them.
5. The portfolio digest's "Waiting for the owner" section uses the same rows: 97 lines become 28.

A project **has a tracker** when any of its cards has a tracker reference or link. Without one,
`require_tracker_log` asks nothing there: no Inbox row, no rule warning at done, and done cards
leave the done column by age. On the copy 37 of 58 done cards are held in the done column (15 past the done window) for this
reason.

### Archive

Exists: **Archive** and **Back from the archive** on a done card (menu and task view), the done
window by age, `task_archive` / `task_unarchive` for coordinators, "Show archived" and search.
On the copy no card was ever archived by hand. `project.archived_at` exists and nothing sets it.

- **A live card can be archived.** Archive on a card in `live` closes it and archives it in one
  step (one confirmation, no rule warning: it is the owner's own act). `task.archived_from`
  (migration 0052) keeps `live`, and Back from the archive returns the card to `live`. On the
  copy 9 of the 24 waiting cards are in `live`, 8 of them for 5 to 10 days, waiting for a look by eye.
  `task_archive` accepts a live card as well.
- **In bulk.** The done column's menu: **Archive all shown** (the count in the confirmation). The
  flash offers **Undo**, which brings back exactly those cards; the events carry one batch id.
- **Projects.** Project settings get **Archive project** / **Back from the archive**;
  `project_upsert` gets `archived`. An archived project leaves the sidebar, All boards, the Inbox
  and the digests; All boards links to "Archived projects". Nothing is deleted. The demo project
  on the copy costs 1 Inbox row and 6 digest lines.

### The plan reminder

By the code (`_reminder_due`, `_plan_reminder`, `reminder_line`): the reminder goes to every
exactly linked session that is the bound session of an open card, for up to 3 cards, in any stage
before done. It is sent at `SessionStart`, after a todo tool's call when the text differs from the
last one sent (the todo item in progress is part of the text, so almost every todo change differs),
and every 30th tool event without that check. Tool calls of a subagent count and receive it too.
`task_create` binds the creating session, so a coordinator that creates a card for another session
is its bound session: on the copy the coordinator session is bound to 5 open cards (three in plan,
one in test, one in review) and gets lines such as "draft, not accepted yet" for a card in review
and "all 5 steps done" for a card that waits for the owner.

Who gets it and when:

- only the bound session of a card in **plan** or **dev**;
- only a line that asks for something: the current or next step, "in dev without a plan", a
  closed plan while the card is in dev. "All steps done" outside dev is not sent;
- never a session named in `coordinators`: it reads drafts that wait in its digests (the existing
  draft hints);
- the comparison with the last text leaves out the "Now doing" part; the periodic repeat is sent
  only for a card in dev;
- a subagent's tool call is skipped when the hook input tells it apart from the main thread
  (unverified: to be checked against a real payload; if it does not, nothing changes there).

On the copy this leaves the coordinator with no reminder, and the 12 open cards with a draft plan
(7 of them in `live`) no longer produce "not accepted yet" lines after dev.

### Binding at start

Measured: 52 `session.launched` events; 31 of the started sessions became known to the board, 19
were ever bound to a card, 14 have their hooks linked. 22 of 52 desktop sessions never linked
their hooks (8 of the 14 created in the last week), so owner comments cannot reach them; 37
guessed hook rows exist next to them. 40 cards had their bound session replaced by hand.

What becomes automatic:

1. **A desktop session exists from its first hook.** A `SessionStart` that names a well-formed
   desktop session id the board does not know (attended, not nested) creates that desktop session
   and links the Claude Code id to it exactly. Today such an id is ignored and a guessed hook row
   is made. With this no one has to pass `hook_session`, comments are delivered from the first
   turn, and the order of the starter's `PostToolUse` and the new session's `SessionStart` does
   not matter.
2. **The launch binds the card.** The `PostToolUse` of the start tool already records
   `session.launched`. It also reads the card keys in the tool's input (strings only, the same
   bounded walk as for the answer; only texts of the form `<known project slug>-<number>` are
   looked at, nothing is stored). Exactly one open card: the new session is bound to it
   (`bound_by` `launch`), and the starter's own binding on that card ends with the reason
   `handed over`. None or several: nothing is bound, and the event says so.
   `session.launched_by` (migration 0052) keeps who started whom. The shape of that tool's input
   is unverified; the walk over all its strings does not depend on field names.
3. **`task_create(bind=false)`** for a session that creates a card for someone else. The default
   stays `true`.
4. **A Claude Code id passed as `session`** (a bare UUID that is the hook id of a known session)
   resolves to that session, with a note, instead of creating a session named by the UUID.
5. **Plan acceptance stays a decision** of the owner or a coordinator. What changes is that a
   coordinator is no longer the bound session of the cards it starts, so `plan_accept` is not
   refused for it, and that drafts after dev are no longer reported as waiting. On the copy 32
   plans are drafts and 16 accepted; 21 cards were closed with a draft.

### Project pulse

On the copy: 3 of 6 projects have no pulse; one busy project's pulse is 9 days old and marked
stale while that project had 43 reports in the last 7 days. A text kept by hand goes stale exactly
when work is busiest.

**Decision: the pulse line is derived from the cards**, with no model: cards moved and reports in
the last 7 days, how many wait for the owner, and the first line of the newest report with its
card and age. It is shown where the pulse is shown today (All boards, Docs & rules, the digests).
The hand-written text stays as an optional **note** under it with its date; it has no stale mark,
and the `Note:` line after a stage move is removed. `project_pulse_set` keeps working and writes
that note; the MCP instructions no longer ask sessions to keep it current. `pulse_stale_days` is
accepted and ignored.

## Data

| Migration | Adds |
|---|---|
| 0051 `backlog_registry` | `backlog_item.kind`, `location`, `origin_text`, `triage_note`, `duplicate_of`, `found_on` |
| 0052 `archive_and_launch` | `task.archived_from`, `session.launched_by` |
| 0053 `document_file` | `document.source_repo_id`, `source_path`, `file_hash`, `file_size`, `file_mtime`, `file_seen_at`; table `document_section` (document, position, anchor, alias, level, title, heading path, first and last line, characters, hash); `import_item.eol`, `bom`, `lead_blank`, `final_newlines` |

The numbers follow the order of the pull requests. `doc_section_index` is an FTS5 table the
service creates and rebuilds, like `search_index`; it is not in a migration. No column or table is
dropped. A start on a new version backs the database up before migrating, as since 0.6.

## MCP contract

Version 2 stays. Added tools: `doc_outline`, `doc_search`. Added optional parameters:

| Tool | Parameters |
|---|---|
| `doc_get` | `lines`, `max_chars` |
| `doc_update` | `source_repo`, `source_path`, `lines`, `expected` |
| `backlog_add` | `kind`, `location` |
| `backlog_update`, `backlog_patch` | `kind`, `location`, `triage_note`, `duplicate_of`, `task` |
| `backlog_list` | `query`, `kind`, `since`, `task`, `has_location` |
| `task_create` | `bind` |
| `project_upsert` | `archived` |

Changed answers, no schema change: `task_context` and `memory_search` (the section pointer),
`portfolio_digest` (the Open tasks section, fewer waiting lines, the derived pulse), `task_list`
(pull requests and the document), `task_archive` (accepts a live card). The one answer that gets
shorter is `doc_get` of a document over 200,000 characters, which could not exist before. The
instructions lose "keep the project pulse current" and gain one line on documents: outline first,
then a section or lines.

## Plan by pull request

| PR | What | Migration | Review | Size |
|---|---|---|---|---|
| 1 | This design; the designs index, ROADMAP (0.14), the site's navigation | none | coordinator | 0.2 |
| 2 | The quiet Inbox: one row per card, done cards out, the tracker rule per project, grouped lines, the badge; the same rows in `portfolio_digest` plus Open tasks; the plan reminder's recipients and moments; the derived pulse | none | reviewer: nothing that needs the owner lost its place | 1.3 |
| 3 | The backlog as a registry: fields, the duplicates note, the link to an existing card, search and filters, the page, the registry import and export | 0051 | reviewer: import (paths, secrets), the migration | 1.2 |
| 4 | Archive and binding: a live card, bulk with undo, projects; the desktop session from its first hook, binding at launch, `task_create(bind)`, a hook id passed as `session` | 0052 | reviewer: hooks and linking, the migration | 1.3 |
| 5 | Documents: file documents, explicit anchors, `doc_outline`, `doc_search`, capped reads and `lines`, the card's pointer, the document page (contents, search) | 0053 | reviewer: file paths, secrets, the index | 1.8 |
| 6 | Exact writes: `replace_section`, `doc_update(lines, expected)`, byte-identical import and export | none (the columns came with 0053) | reviewer: import and export | 0.5 |
| 7 | Release 0.14.0 | | | 0.2 |

Each pull request builds and can ship alone; SPEC, `docs/mcp-tools.md`, the generated reference
and CHANGELOG change in the pull request that changes the behaviour. Tests per pull request:

- 2: `tests/test_navigation.py` and `tests/test_tracker_archive.py` (each row class of the table
  above as a fixture, with the count before and after), `tests/test_digests.py`,
  `tests/test_plan.py` and `tests/test_hooks.py` (who gets the reminder, in which stage, a
  coordinator never).
- 3: `tests/test_backlog.py`, `tests/test_import_export.py` (a registry fixture with long cells,
  both date forms and free decisions; an import of the export changes nothing),
  `tests/test_mcp_contract.py`.
- 4: `tests/test_task_archive.py` (live and back to live, bulk and undo), `tests/test_sessions.py`
  and `tests/test_hooks.py` (a first hook with an unknown desktop id, nested and unattended starts
  still not linked, a launch with one, no and two keys), `tests/test_migrations.py`.
- 5: `tests/test_docs_in_db.py` and `tests/test_memory.py` with a generated document of 900,000
  characters (the outline's size, every capped read under the cap, a hit per section, a path that
  leaves the repository, a file that disappears, a masked line), `tests/test_board.py` for the page.
- 6: a property test (every section replaced by itself leaves the text equal) and fixtures with
  CRLF, a byte order mark, no final newline and trailing blank lines that export with the same
  SHA-256.

## Risks

- **The Inbox hides something that needed the owner.** Each class that leaves keeps a place: the
  card (warnings, replies), Sessions (unlinked), the done list (work log). Pull request 2 ships
  the counts before and after as a test, and its report lists what moved where.
- **A project wrongly taken as "without a tracker".** One card with a tracker reference turns the
  requirement on for the project; the rule is named in the help text of the Inbox line.
- **Binding the wrong card at launch.** Only an exact, single, open card key binds; the event
  names how it was bound, and `task_bind_session` still rebinds.
- **A first hook creates a session for an id nobody announced.** Only a well-formed id from an
  attended, not nested start does; the request comes through the same token as every hook.
- **relaybay reads files of a repository.** Only a path under a repository the project names, no
  link out of it, a size limit, read only. The index stores text: the secret detectors mask
  matching values in it.
- **The file on disk is one branch.** The read names the branch and the content hash; the pointer
  is repository-relative, so a session reads its own tree.
- **A reworded heading breaks a card's link.** Explicit anchors; a broken link is marked on the
  card and in `task_context`, as today.
- **Clients with a cached schema** see the new parameters only after a reconnect; nothing they
  send today is refused.

## Decided points

Defaults by the most reversible way; the other way is one change later.

| # | Point | Default | Other way |
|---|---|---|---|
| K1 | Source of truth of a document edited with code | the file in its repository, indexed | the database with a sync to the file |
| K2 | Writing a file document through relaybay | never; the refusal names the file and the lines | `doc_update` writes the section into the file after a hash check |
| K3 | A project without a tracker | derived: no card of it ever had a tracker reference | a switch per project |
| K4 | Archive on a live card | closes and archives in one step, comes back to live | only done cards; the owner moves the card first |
| K5 | Binding at launch | reads card keys from the start tool's input, nothing else, nothing stored | never read the input; bind by hand |
| K6 | Plan acceptance | stays by hand; no "not accepted yet" after dev | a project rule accepts S and M plans by itself |
| K7 | Pulse | a derived line; the hand text stays as a note with its date and no stale mark | drop the hand text; or keep it as it is |
| K8 | Done cards in the Inbox | none; warnings and replies stay on the card | listed for the done window |
| K9 | A secret detector's hit in a file document | the value masked in the index, the document accepted | refused until the policy allows the line |
| K10 | Default read cap for a document over 200,000 characters | 40,000 characters | the client's own limit only |
| K11 | Backlog kinds | `bug`, `idea`, `question`, `debt`, empty for old items | free text |

## Verification (0.14)

On a copy of a working board, never on the live data folder:

- the Inbox badge shows the cards that wait (24 on the measured copy, from 99), each card once
  with its marks; no done card, no ended session, no card of an archived project;
- a card in live is archived and brought back to live; the done column is archived in one action
  and Undo restores it; an archived project is gone from the sidebar and the digests and comes back;
- a coordinator session that created and started cards receives no plan reminder; a session
  working a card in dev receives its current step and nothing after the card left dev;
- a session started from another one appears as a desktop session with linked hooks and, when the
  start named one card, bound to it, with no `task_bind_session` and no `hook_session`;
- a 900,000-character markdown file in a repository is added as a file document: `doc_outline`
  under 10,000 characters, no `doc_get` answer over the cap, `doc_search` returns different
  sections for one query, `task_context` of a linked card names the section's size and lines and
  stays under its budget, and the file's SHA-256 is unchanged after all of it;
- the document page shows the contents next to the text and finds a word inside the document;
- a registry table of over 100 rows imports in a dry run with no row refused for its length, date
  or decision, and an import of the export changes nothing;
- `uv run ruff check` and `uv run pytest` are green on three systems.

# 0008 — Memory and context

- Status: **Accepted** (step 11.6), planned for 0.3.0 together with Review
  ([0007](0007-review.md)). 11.6a and 11.6b are built: [SPEC §8](../SPEC.md#spec-memory).
- Size: 11.6a ~3 agent-days, 11.6b ~2, 11.6c ~1.5 (includes the revision after research: time axes,
  candidate operations, blocks, stemmed search, secret scan, SessionStart injection); optional
  semantic search ~1.5 later.

## Problem

Coordinators (a coordinator over all projects, a manager per project) and task sessions lose context:
after `/clear` or compaction they re-read files, read the wrong level of detail, or miss decisions.
The knowledge lives in markdown files spread over folders (briefs, task registry, status and spec
files, task notes, memory notes). Goal: every role gets **the right level of information first**,
can **drill down on demand**, and **writes back** in a way the next reader understands, without
relaybay running its own model.

## Principles

1. **Levels.** Read top-down, write bottom-up.
   - Portfolio (coordinator): projects, priorities, people, what waits for the owner, decisions of the week.
   - Project (manager): tasks and stages, spec, environments and access, project decisions, history.
   - Task (session): the task, its decisions, linked spec sections, environment map, rules, unread
     comments.
   Summaries go up (task progress → project pulse → portfolio digest); briefs go down.
2. **One place per fact.** The database is the source of truth; markdown is the *format* of documents
   and notes, not their storage. Files are exports and views.
3. **Agents summarise, relaybay assembles.** No model in relaybay: it stores, versions, searches and
   assembles context deterministically; summaries are written by agents under the rules.
4. **Changes since last read.** Every reader has a cursor; digests show what changed since then.
5. **Provenance and decay.** Every decision and note has an author, a source and dates; old notes get
   a "verify" mark instead of silently going stale; superseded items stay in history.
6. **Budgeted context.** Each digest has a size budget (default ~3k tokens, also in characters) and
   links for drilling down. Never dump everything.

## What goes where

| Kind | Examples | Storage |
|---|---|---|
| State | projects, tasks, stages, sessions, bindings, environments, contacts | tables |
| Decisions | "invites auto-accept for existing users (owner, chat)" | `decision`: scope (global / project / task), body, structured source, author, time axes, `superseded_by`; append-only |
| Episodes | reports, events, comments, hook activity | existing tables; read through digests |
| Documents | spec, status, brief, runbooks, ADRs | `document` + `document_version` (markdown, sections with anchors, author, reason, base version) |
| Memory notes | owner preferences, feedback, references | `memory`: scope (global / role / project), kind (user / feedback / project / reference), title, body (fact + why + how to apply), source, links, `review_at`, time axes, read tracking |
| Blocks | "owner preferences", "project pulse" | `memory_block`: label, description, character limit, read-only / owner-only flag; always in the level's digest |
| Rules | report format, stage requirements | rules ([0002](0002-working-rules.md)) |
| Stays a file | `CLAUDE.md`, skills, agents, code docs in repositories | disk: Claude Code loads them from disk |
| Never stored | secrets | only "where it lives" ([0006](0006-environments-and-access.md)) |

**Two time axes** on decisions and notes: `valid_from` / `valid_to` (when the fact was true) and
`recorded_at` / `superseded_at` / `superseded_by` (when it was recorded or replaced). Superseding
invalidates, never deletes; "what was true on date X" is answerable.

**Structured provenance:** `source` references a report, event, comment or external message id, plus
display text.

**Read tracking:** `last_read_at`, `read_count`; digests flag notes never read or past `review_at`.

## Reading: digests per level

All with a `since` cursor per reader, a size budget and drill-down links; **index lines, bodies on
demand** (one line per note or decision: title + hook + id).

- `portfolio_digest()` — coordinator: one line per project (stage counts, pulse, stale flag), the Inbox,
  decisions of the last 7 days, tasks without progress for N days, global blocks, changes since the
  last read.
- `project_digest(project)` — manager: pulse, tasks by stage with next step and waiting on, open
  questions, recent decisions, environments summary, project blocks and notes, changes since the
  last read.
- `task_context(task)` — session at start or after `/clear`: task fields and progress, task and
  project decisions, linked spec sections by anchor, environment map, effective rules, unread
  comments, relevant notes (scope + tags).
- Drill-down: `relaybay://doc/<project>/<doc>#<anchor>`, notes and documents as
  `relaybay://…/<slug>.md` resources (file-shaped), `decision_list(scope, query)`,
  `memory_search(query, scope)` (snippets with line references), `events_since`.

**SessionStart injects context.** On startup, `/clear` and compaction the hook answer returns the
level's digest or `task_context` as `additionalContext` (the session id is already returned there
today, see [SPEC §4](../SPEC.md#spec-hooks)), so restoring the picture does not depend on the model
remembering to call a tool. Server instructions add: "start with the digest for your level; drill
down only when needed."

## Writing

- `decision_add(scope, body, source, supersedes?)`; superseding keeps the old one visible as
  replaced.
- `memory_add` first returns the top candidate notes; the agent answers with `op` = `add` / `update`
  / `supersede` / `noop`; adding next to a strong match requires `force_new`.
- `memory_update`, blocks and `doc_update(doc, section?, body, reason, base_version)` use optimistic
  locking; a conflict returns the newer version to merge.
- `project_pulse_set(project, text)`: a ≤5-line project summary, asked for by the rules on every stage
  change in the project; a pulse older than N days is flagged in the portfolio digest.
- Rules check: a decision has a source; report first line; pulse freshness (warn).
- **Secret scan on write:** notes, decisions and documents with key or token patterns are refused
  with a clear message.
- Visibility: the coordinator reads everything; a manager its project plus global notes; a session its
  task context. Global memory and rules change only by the owner or the coordinator (proposals
  otherwise).

## Search

- **v1: SQLite FTS5** over documents, decisions, notes, reports and comments; external-content
  tables with sync triggers and `rebuild`. A `unicode61` column plus a Snowball-stemmed shadow column
  (pure-Python `snowballstemmer`, English and Russian) for word forms, a trigram index for partial
  identifiers (≥3 characters), bm25 column weights. No server, exact names, keys and error texts are
  found reliably.
- **Vectors — optional extra `relaybay[semantic]`, off by default.** Turned on only when one is
  true: intake of chats and e-mail; searchable transcript archive; a corpus over a few thousand
  items or frequent paraphrase questions; cross-language lookups. `sqlite-vec` in the same database
  file plus a small local multilingual model (default `intfloat/multilingual-e5-small`, 384 dims, via
  fastembed; fallback `paraphrase-multilingual-MiniLM-L12-v2`); hybrid ranking FTS + vector by
  reciprocal rank fusion (configurable k, default 60). Embeddings are data (vector, model id,
  version, content hash); the `vec0` table is a rebuildable index partitioned by project; a model
  change re-embeds. sqlite-vec is pre-v1 and brute force (fine to ~100k vectors) and cannot load in
  the macOS system Python: the server falls back to FTS when the extension does not load. Vectors
  switch on only if they beat FTS on a small eval set (~30 real queries with expected hits).
  Embeddings are computed locally; nothing is sent to external services.
- Not for vectors: state, stages, rules, decisions by project — those are queries.
- No knowledge graph: unversioned graphs decay.

## Migration of an existing markdown setup

`relaybay import` maps project briefs → project document + decisions + contacts; task registry →
tasks; per-task notes → task description, decisions, documents; a memory folder → notes (global
scope); a retro registry → a document. Dry-run diff first, idempotent re-run. `relaybay export`
writes documents, decisions and notes back to the same layout (optionally daily) for git history,
backup and grep. After acceptance a coordinator's own instructions shrink to "start with
`portfolio_digest`", and the markdown folder becomes the export target.

## As built in 11.6a

- Decisions have scope `project` or `task` only: `decision.project_id` stays NOT NULL (the migration
  is additive), so global facts are global memory notes. A global decision scope needs a table
  rebuild later.
- Search is one FTS5 table kept in sync by the service in the write's transaction (stems computed
  in Python with `snowballstemmer`), not external-content tables with triggers; no trigram index
  yet. Without FTS5 search falls back to `LIKE`.
- Visibility per role is not enforced yet.

## As built in 11.6b

- The pulse lives on the project row (`pulse`, `pulse_at`, `pulse_by`), not in a block: it has its own
  limit (5 lines, 600 characters), its own event and its own age. Rules do not ask for it; a stage
  move through MCP with a missing or stale pulse adds one soft `Note:`, once per project per server
  run.
- `domain/digest.py` turns gathered data into sections and budgeted text without I/O; `service.py`
  gathers. Budgets are in characters only. A section has a priority apart from its place in the
  text: the changes come last in a project digest but do not shrink first.
- The reader is the session's desktop id, else its name; `reader_cursor.ref_id` is 0 for the portfolio.
  A read that finds nothing new does not move the cursor, so a session start without news needs no
  write transaction; neither does a budget cut in the changes, so nothing is lost.
- The digests are annotated as writes (`session` moves the read position and can create or link the
  session row), unlike `memory_get`, whose read tracking changes nothing a caller depends on.
- Relevant notes are ranked by search on the task's title, without tags.
- Blocks show one clipped line per block in a digest; `block_get` has the rest.
- Not built: visibility per role, markdown import and export (11.6c), vectors.

## Order

1. **11.6a** — `document` / `document_version`, decision scopes and supersede, `memory` and blocks,
   FTS5, MCP write tools, Docs & rules page (view, history, diff).
2. **11.6b** — digests, reader cursors, budgets, pulse and stale flags, SessionStart injection,
   instructions update.
3. **11.6c** — import and export of the markdown layout, dry-run diff, fixtures from a neutral demo
   setup.
4. Later, optional — `relaybay[semantic]`, enabled with intake.

## Risks

| Risk | Mitigation |
|---|---|
| Digests grow until nobody reads them | Budgets in tokens and characters, index lines, bodies on demand |
| Duplicate and contradictory notes | Candidate operations before add, supersede with time axes |
| Stale facts | `review_at`, read tracking, "verify" marks |
| Secrets in memory | Secret scan on write |
| Two sources of truth during migration | Import is idempotent with dry run; export keeps markdown readers working |

## Sources

- anthropic.com/engineering/effective-context-engineering-for-ai-agents
- platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- code.claude.com/docs/en/memory
- docs.letta.com/guides/agents/memory-blocks
- docs.mem0.ai/core-concepts/memory-operations/update
- help.getzep.com/graphiti
- langchain-ai.github.io/langmem/concepts/conceptual_guide
- github.com/modelcontextprotocol/servers/tree/main/src/memory
- github.com/asg017/sqlite-vec
- sqlite.org/fts5.html
- qdrant.github.io/fastembed

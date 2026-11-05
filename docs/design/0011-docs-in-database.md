# 0011 — Project documents in the database and markdown import

- Status: **Implemented** (unreleased), for 0.4.0. Shipped behaviour:
  [SPEC §8](../SPEC.md#spec-documents-kinds); what differs from this page: [As built](#as-built).
- Size: to be estimated with the plan; it builds on the document storage and import of
  [0008](0008-memory-and-context.md).

## Goal

The working documents of a project — spec, status, per-task notes and decisions — live in relaybay
with versions and can be read and edited by agents through MCP, so a coordinator that today keeps
them as markdown files can move over completely.

## Scope

1. **Document kinds per project:** SPEC (how it is built, with stable section anchors), STATUS (the
   owner's map: what runs where, branches in flight), per-task document (what was asked, decisions,
   work log), decisions. Each with versions (author, reason, base version, diff).
2. **Editing via MCP:** read by section anchor, `doc_update` with optimistic locking
   ([0008](0008-memory-and-context.md)), links from tasks and reports to sections.
3. **UI:** project → Docs & rules: view, history, diff, edit; a task's document in the task dialog.
4. **Import of markdown registries:** an existing task registry table (project, task, stage,
   waiting on, next step, document) → tasks, with column aliases in any language, the leading stage
   word mapped through an alias map and the rest kept as the stage note; per-task markdown → task
   document; dry-run diff first, idempotent re-run.
5. **Moving a coordinator over:** its instructions point at relaybay instead of files; the markdown
   folder becomes the export target.

## Open before planning

- Which document sections are structured (status table, decision list) and which stay free markdown.
- How repository-owned documents (that must stay in a repository) are linked rather than imported.
- Overlap with the memory step's `document` tables and import: reuse them, add only what is
  specific to project documents.

## Risks

| Risk | Mitigation |
|---|---|
| Two sources of truth during the move | Import is idempotent with dry run; export keeps file readers working |
| Formats of existing markdown vary too much | Alias maps, import by heading names, unknown parts kept as free text |
| Lost edits from concurrent agents | Optimistic locking with base version |

## As built

Most of the scope came with the memory step ([0008](0008-memory-and-context.md), 11.6a and 11.6c):
documents with versions, section anchors, `doc_update` with optimistic locking, Docs & rules with
history and diff, the markdown import (registry with column and stage aliases in any language, task
folders as `task-<ref>` documents, dry run, idempotent re-run) and the export. This step reuses that
storage and adds:

- **Kinds.** `spec`, `status`, `task`, `brief`, `runbook`, `adr`, `retro` are the well-known kinds;
  any other kind stays free text. Docs & rules groups the documents by kind, the well-known ones first.
  Import names a `docs/<doc>.md` file's kind after these too.
- **Open question 1, decided:** documents are free markdown. A status table or a decision list is not a
  structured section; decisions already have their own table with supersede chains.
- **Open question 2, decided:** a repository-owned document is linked, not imported. `document.source_url`
  (migration `0020_document_source`, additive) holds its http(s) link; the board shows "lives in the
  repository" and offers no edit, `doc_update` refuses it without `force`, and its body may be empty.
  Both directions that drop a copy need `force`: unlinking (any write to a linked document) and
  linking an existing document with body `-`, which empties its text; linking while keeping the text
  does not. Links are checked like `tracker_url`: http or https, no control or invisible characters.
  Export skips such a document when it has no text and writes it as usual when it has; import skips a
  changed file of one, so the repository stays the source.
- **Open question 3, decided:** no second storage; only `source_url` is new.
- **Editing on the board:** Edit on the document page, or the edit link of a heading for one section,
  opens the markdown with a required reason; a save is `doc_update` with the base version (cookie and
  CSRF). A conflict keeps the typed text and shows the current text to merge. New document on Docs &
  rules.
- **Links:** `relaybay://doc/<project>/<doc>#<anchor>` in a task's description, progress, reports and
  comments becomes a board link when the page renders; a missing section links to the document with a
  mark, a missing document stays text with a mark. One read per linked document per page. A task whose
  document field is such a link shows the document (or that section) in a collapsible panel of the
  task dialog.
- Heading ids and edit links come from the same section list as `doc_get` and `doc_update`, matched
  by line; a heading that list does not count (setext, inside a quote) gets neither, so a link or an
  edit never lands on another section. After a conflict the edit page shows the current text and
  the base version from one read; a section gone meanwhile turns it into a whole-document edit.
- Links inside code spans stay code. The task dialog panel reads the document once per render; it is
  not refreshed apart from the dialog.
- The coordinator's move is a how-to: [Coordinate many projects](../how-to/coordinator.md).

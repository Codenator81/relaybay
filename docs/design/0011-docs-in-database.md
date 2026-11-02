# 0011 — Project documents in the database and markdown import

- Status: **Accepted** (scope and order). A detailed plan and estimate are written before the step
  starts. Planned for 0.4.0, after screenshot annotations ([0009](0009-screenshot-annotations.md))
  and before the runner ([0012](0012-runner-and-embedded-terminal.md)).
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

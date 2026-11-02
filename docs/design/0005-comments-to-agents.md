# 0005 — Comments from the board to agents

- Status: **Accepted** (step 11.4), planned for 0.2.0. Needs the hooks of
  [0001](0001-hooks-and-setup.md).
- Size: ~1.5–2 agent-days; +0.5–1 if the `asyncRewake` wake-up works and is adopted.

## Goal

A comment written on a card reaches the agent of the bound session; the card shows whether it was
delivered and read.

## Model

`comment`: id, task_id, author (owner or session), body_md, created_at, delivered_at,
delivered_to (session), read_at. Later extended with optional `step_id` / `attachment_id` for
review steps and screenshots ([0007](0007-review.md)). Events `comment.added`, `comment.delivered`,
`comment.read`.

## Delivery

- **Working session** (documented hook behaviour): the hook receiver answers `PostToolUse` /
  `PostToolBatch` / `UserPromptSubmit` with `hookSpecificOutput.additionalContext` containing the
  unread comments for that session's task ("Owner comment on acme-shop-12: …; ack with
  comment_ack") → delivered. A `Stop` hook answers `decision: "block"` with the comments as `reason`
  when unread ones exist, so the agent handles them before ending the turn; guarded by
  `stop_hook_active`, the client's continuation cap and relaybay's own counter (at most twice per
  turn).
- **Idle session:** by default the comment waits for the next `UserPromptSubmit`; the coordinator
  sees `comment.added` in `events_since` and can forward it. To verify before building, because they
  would wake an idle session: an `asyncRewake` hook started at `SessionStart` that long-polls
  relaybay and exits with code 2 when a comment arrives; Claude Code channels; the session's
  cross-session inbox (reachable only from processes started by that session, so again via a hook).
- **Sessions started by relaybay** ([0012](0012-runner-and-embedded-terminal.md)) receive comments
  directly.
- Marked read when the agent calls `comment_ack` or replies.

## API

`comments_list(task, unread_only?)`, `comment_ack(id)`, `comment_add(task, body)` (agents can reply).
Hook endpoint gains `PostToolUse`, `PostToolBatch` and the `Stop` answer; `relaybay setup` adds
the extra hook events (the marker rules stay the same).

## UI

Comment box in the task dialog; list with "sent / delivered / read" marks, live over SSE; a small
unread counter on the card. Unanswered agent replies appear in the Inbox
([0004](0004-navigation.md)).

## Risks

| Risk | Mitigation |
|---|---|
| A `Stop` block loops | `stop_hook_active`, own counter, client cap |
| Comments injected into the wrong session | Delivery only to the exactly linked hook session of the bound session |
| Comment text as prompt injection | It is the owner's own text; agents treat it like a user message; length limit |
| Idle sessions never see a comment | Shown as "sent, not delivered"; coordinator forwards; wake-up options verified first |

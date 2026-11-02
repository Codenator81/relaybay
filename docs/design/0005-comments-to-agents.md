# 0005 — Comments from the board to agents

- Status: **Implemented** (step 11.4, unreleased, for 0.2.0); the behaviour is in
  [SPEC §1](../SPEC.md#spec-comments), [§2](../SPEC.md#spec-board-comments) and
  [§4](../SPEC.md#spec-hooks-comments). Needs the hooks of [0001](0001-hooks-and-setup.md).
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
  sees `comment.added` in `events_since` and can forward it. Options considered for waking an idle
  session: an `asyncRewake` hook that long-polls relaybay and exits with code 2 when a comment
  arrives (first thought of as started at `SessionStart`; built started at `Stop`, see the decisions
  below); Claude Code channels; the session's cross-session inbox (reachable only from processes
  started by that session, so again via a hook).
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

## Decisions taken while building

- **Exact link only.** A new `session.cc_exact` marks hook ids the agent linked itself
  (`hook_session`); links guessed from the folder (and every link made before migration `0008`) get
  no comment text until the agent passes `hook_session` once.
- **Delivery events:** `UserPromptSubmit`, `PreToolUse` and `PostToolUse` (added to the installed
  events). `PostToolBatch` is not installed: `PostToolUse` already covers every tool call.
  Comments fit in about 8000 characters per answer (Claude Code moves `additionalContext` over
  10,000 characters into a file); the rest follow with the next events.
- **Linking is the caller's own.** An id another live session linked exactly is never moved (the
  write answers with a warning line), and `task_bind_session` links no hook id at all, so a
  coordinator binding a session cannot pull that session's comments into its own context.
- **Rebind** hands delivered-but-unread comments to the newly bound session.
- **Stop semantics.** relaybay blocks at most twice per turn per session and twice per comment in
  all (then the SessionStart line and comments_list remind), and never stacks onto a
  continuation it did not start: with `stop_hook_active` set and no relaybay block in this turn it
  allows. Its counter resets on `UserPromptSubmit` and whenever it allows a `Stop`, because a turn
  woken by the wake-up hook starts without a prompt.
- **Read marks.** An owner comment is read on `comment_ack` or a reply (`in_reply_to`, else every
  unread one of the task). An agent reply is read when the owner presses "Mark replies read" or
  writes a comment on the task.
- **asyncRewake: built as an opt-in, not verified live.** The hooks reference documents
  `asyncRewake` as a command-hook field ("runs in the background and wakes Claude on exit code 2";
  stderr, or stdout when stderr is empty, is shown to Claude as a system reminder), says an
  `asyncRewake` hook that exits 2 wakes Claude even when the session is idle, and that Claude Code
  still enforces `timeout` on it (default 600 s for command hooks). That is enough to wire it
  safely: `relaybay setup --wake` (default off, command mode only) adds a separate, marked `Stop`
  hook that long-polls `/hooks/claude-code/wait` for up to 25 minutes with a 1800 s hook timeout and
  exits 2 only with comment text, 0 in every other case. Starting it at `Stop` instead of
  `SessionStart` re-arms it after every turn; a newer wait supersedes the older one. Tested against
  a real relaybay server with the hook command run as a process; not verified against a live
  Claude Code (the reference names no minimum version for `asyncRewake`). Because a build that
  ignored the field would hold every turn's Stop for up to 30 minutes, setup refuses `--wake`
  without `--force-wake` while `MIN_ASYNC_REWAKE_VERSION` is unknown, and `doctor` shows the
  installed hook as `UNVERIFIED`. The reference documents no hook input field that tells a hook it
  runs in the background, so the client cannot detect the fallback itself. Without it, a comment
  waits for the next prompt, and the coordinator sees `comment.added` in `events_since`.
- Not built in this step: the unread counter on the card, channels and the cross-session inbox.

# 0019 — Messages between sessions

- Status: **Implemented (unreleased)**, for 0.8.0. The behaviour is in
  [SPEC §1](../SPEC.md#spec-session-messages), [§2](../SPEC.md#spec-board-messages) and
  [§4](../SPEC.md#spec-hooks-session-messages). Comes from
  [0017](0017-readiness-for-agents-inside.md). Needs the hooks of
  [0001](0001-hooks-and-setup.md) and the delivery of [0005](0005-comments-to-agents.md).
- Size: about 2.5 agent-days.

## Problem

Sessions talk to each other outside relaybay: a desktop `SendMessage` is an edge on the Map, and its
text is never read by design. A coordinator inside relaybay needs a channel it can see, and the owner
needs to see what was asked of whom and whether it arrived.

## Decision

A new MCP channel with its own table, `session_message`. A desktop `SendMessage` stays what it is: its
text stays unstored (owner's default).

- `message_send(body, to?, task?, in_reply_to?, session?, session_title?, hook_session?)`,
  `messages_list(session, box?, unread_only?, task?, limit?)`,
  `message_ack(message_id?, session?, session_title?, hook_session?)`.
- Delivery as for owner comments: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, the `Stop` block and
  the wake-up hook; exact links only; a spool replay gets no answer. Comments come first; messages
  share what is left of the 8000 characters of one answer. A body is one quoted literal, so no line of
  it can stand on its own and read as an owner comment.
- Each send also emits `session.message`, so the Map works unchanged. Events `message.sent`,
  `message.delivered` and `message.read` hold names and marks, never the text; the text is not in
  full-text search and not in the Markdown export.

## Who may send

All MCP clients share one token, so nothing here is access control. What it does stop is a session
passing for another by mistake or by a casual guess:

| The call says | Result |
|---|---|
| Neither `session` nor `hook_session` | Refused: the sender is required |
| A name any session with a live exact link goes by (desktop titles and aliases too), without that link's id | Refused; the error names the way out for a restart with no session end: another write with the new `hook_session` moves the link |
| Any name with a hook id another live session linked | Refused, and the refused write changes no row |
| A name no live hook link proves (no hooks, ended, or a made-up id written on a row) | Accepted, stored and framed as "name not verified", and the caller is told |
| The session's own name and its own live hook id | Verified |

Not proven by this: a client holding the token can still post a made-up hook event, move a link with
another write (a link's move on other tools is how a restarted terminal session gets its row back) or
read a hook id from the event feed. That is why a message never grants anything: the frame says it
comes from another session and not the owner, and that it does not change the reader's level.
A message's text never reaches a session as anything else than one quoted literal, in
`messages_list` as in a hook answer, and a name is one line.

`message_ack` creates no row. On a session with an exact link, live or ended, it needs that link
proven, or a message acknowledged in its name would never be handed over when it comes back; a
reply marks its parent read under the same rule. A row that never linked itself cannot prove
anything: a call naming it can still acknowledge its mail, and the mark says "read (name not
verified)". Such a message is not handed over later if that session links after all. So anyone
with the token can mark the mail of a hook-less row read, visibly, and not the mail of a linked
one by naming it. Reading with `messages_list` needs only the token, like everything else on the
board.

## Who receives

`repo.find_sessions` plus the Claude Code id, never `_session`, which would create the row a typo names.
None is an error; several are an error listing the desktop ids and statuses, never a hook id.
A recipient no hook can reach is said to be so, instead of a success that hides it:

| Recipient | Answer |
|---|---|
| Live and exactly linked | "Not delivered yet: it reaches it at its next tool call or prompt." |
| Idle | "Not delivered yet: it is idle. It sees this at its next prompt or wake-up." |
| Never linked itself (no hooks, or guessed from the folder) | "Not delivered: it never linked its own hook session … " |
| Ended | "Not delivered: it has ended; this waits in case it comes back." |

A guessed row merged into another (`_link_hook_session` deletes it) hands its messages over first, or
they would lose their recipient. A row that was exactly linked keeps its mail: naming the hook id of
an ended session takes the link, not what was written to that session. The exception: an exactly
linked row of kind `hook` that is deleted when another row takes over its ended id does not hand its
mail over. Those messages keep the names, lose the recipient row, stay unread and say "its session row
is gone".

## Size

A body is at most 4000 characters, and line separators become line breaks, because quoting would
make each of them six characters. A body whose quoted form would not fit in one hook answer with
its frame (line breaks, quotes and backslashes count double) is refused when it is sent, so a
delivery or a Stop reason stays within the 8000 characters.

## Loop guards

Ten sends per sender per minute (a tool error; a refused send does not count). A reply deeper than 8 is
refused, and the message it answered shows in the Inbox until the owner marks it seen. A Stop block
once per message, inside the per-turn cap of the comments. A session cannot write to itself. A client
that changes its name per call escapes the per-sender count; it is a guard against loops, not against
abuse.

## Board

`/messages` in the sidebar with sent, delivered and read marks, "not delivered yet" with its reason, a
session filter. A Messages block in the task view, an unread count on Sessions rows. Names and bodies
are agent-supplied: they are plain autoescaped text, never markdown. Live updates refetch the whole
fragment from the server, because marks change on rows already shown; no script builds a row.

## Consequences

Stop blocks now also come from another agent. The cap keeps that from running a session in circles, and
the Stop reason says who it is from. The idle case is unchanged from comments: without the wake-up hook
a message waits for the next prompt, and the board says "not delivered yet".

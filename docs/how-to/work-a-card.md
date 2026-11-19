# Work a card with a session

relaybay starts no agent. A card is worked by a Claude Code session you open yourself, in the
Claude desktop app or in a terminal (`claude`); both are first-class. The board keeps the record
and tells you when the session needs you. This page is the everyday loop.

Before the first time: the MCP server is added and the hooks are installed
([Connect Claude Code](../connect-claude-code.md), steps 1 to 3).

## 1. Create or pick a card

Open the project's board (`acme-shop` in the examples) and pick a card. A new card comes from a
session (`task_create`, with a short description: what to do and why; a coordinator session
often writes them) or from the backlog, where **To plan** turns an item into a card.

## 2. Copy the brief

Open the card. While no session works on it (the state line says **No session** or **Ended**), the
line has **Copy the brief**. The brief holds what a new session needs: the card's key and title, the
line that tells it to bind itself, next step, description, done so far, the plan and its current
step, decisions, linked documents, environments, rules, attachments by path and the report line.
Where the browser gives no clipboard, the fold under the line opens with the text selected.

## 3. Paste it into a new session

Start a new session in the project's folder, in the Claude desktop app or in a terminal:

```bash
cd ~/code/acme-shop
claude
```

Paste the brief as the first prompt. The session binds itself to the card with
`task_bind_session` and passes the `hook_session` relaybay gave it at its start, so the hooks link
its status, comments and plan to the card. Nothing else to set up: from now on the card shows the
session.

A session in the Claude desktop app has its hooks linked from its first hook, without
`hook_session`. One a coordinator starts with the desktop app's start tool, with this card's key in
its prompt, is bound to the card at once
([Binding at start](coordinator.md#binding-at-start)).

## 4. Watch the state line

The state line on the card, and its word on the board, say what the session does:

- **Working**: busy, with its last activity;
- **Idle**: its turn ended;
- **Waits for you**: an open question or approval request, a review to accept, "waiting on" naming
  you, or the session asking for its user; the reasons are listed under the word;
- **Ended**: the session is gone; **Copy the brief** is back for a new one.

Under the state line come the last report, then the comment thread.

## 5. Answer and comment

- **Questions and approvals** a session asks wait in the **Inbox** and on the card. Your answer or
  decision reaches the session as an owner comment through its hooks.
- **Comments** you write on the card reach the session through its hooks: with its next prompt or
  tool call, or, with the opt-in wake-up hook, even while it is idle
  ([Comments from the board](../connect-claude-code.md#3-live-session-status-hooks)). When one has
  not arrived, the comment says why, for example "the session ended" or "the session is idle and the
  wake-up hook is not waiting".

## 6. Review by eye

Before the card moves to `review` or `test`, the session writes what you should check
(`review_steps_set`) and attaches screenshots. Accept or reject each step on the card's Review tab;
each decision reaches the session as a comment. The Inbox lists the cards waiting
for you with the mark "to accept n/m".

## 7. Open the session again

**Open** in the state line is a menu that takes you back to the session:

- **In desktop** opens a desktop session in the Claude desktop app, live or ended. A session
  started in a terminal cannot be opened there.
- **In terminal** resumes an ended session, from the desktop app or a terminal, with `claude
  --resume` in a new terminal window, in the folder it started in; only when the board is opened on
  the same machine. While the session is live it is disabled ("running in the desktop app",
  "running in a terminal"): switch to its window yourself.

A disabled item stays in the menu with its reason.

A resumed terminal session is bound again to the cards its end unbound. Details:
[Connect Claude Code → Watch the board](../connect-claude-code.md#4-watch-the-board).

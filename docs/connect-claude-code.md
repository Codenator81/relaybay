# Connect Claude Code

relaybay runs one MCP server for all your sessions. [Install it](getting-started/install.md) and
start it first: start the desktop program (it runs the server inside), or without it:

```bash
relaybay serve
```

## 1. Add the server

`relaybay token` prints the token and the exact command:

```bash
claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

`--scope user` stores the entry once in your user configuration, so every repository sees it and
nothing is written into the repositories themselves. Check it with `claude mcp list`, or with
`/mcp` inside a session.

### Alternative: `.mcp.json`

To keep the token out of the configuration file, put it in an environment variable
(`RELAYBAY_TOKEN`) and reference it; Claude Code expands `${VAR}` in `url` and `headers`:

```json
{
  "mcpServers": {
    "relaybay": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${RELAYBAY_TOKEN}" }
    }
  }
}
```

Project-scoped servers from `.mcp.json` ask for approval once in interactive sessions.

After `relaybay token --rotate`, restart the server (`relaybay serve`, or **Restart server** in
the desktop program's tray) and update the token in the entry. When the program is the client of
an external server (`server_mode = "external"`), `token --rotate` is refused: rotate the token
where that server runs.

## 2. Tell the agent when to use it

Add a short section like this to the project's `CLAUDE.md` or `AGENTS.md` (replace the slug):

```markdown
## Task board (relaybay)

This project is `acme-shop` on the relaybay board (MCP server `relaybay`).

- At the start of a session, call `project_digest(project="acme-shop")`; for more detail
  `project_snapshot`, whose first line names the working rules (read them with
  `rules_get(project="acme-shop")`).
- Before starting work, find the task with `task_list` or create it with `task_create` (with a
  short `description`: what to do and why); then call `task_bind_session` with your session name.
- When the work moves on, call `task_set_stage` (plan → dev → review → test → live → done).
- Keep "waiting on" and "next step" current: pass `next_step` and `waiting_on` with every
  `task_set_stage` and `report_add` (or use `task_update`).
- At the end of every stage, call `report_add` with your final report. Start it with a line
  `state · size · gist`, e.g. `verified · M · checkout works`, and pass `progress`: the updated
  summary of everything done on the task so far.
- When you stop working on a task and nobody takes over, call `task_unbind_session`.
- Record decisions the user makes with `decision_add` (with `source`; `supersedes` when one
  replaces an earlier decision). Keep lasting facts with `memory_add` and project documents with
  `doc_get` / `doc_update`; look things up with `memory_search`.
- Pass `session` on every write: your own desktop session id (`local_…`) if you know it, with
  `session_title=<your session title>`; otherwise your session name. If the session start told
  you "Your relaybay hook session: <id>", also pass `hook_session=<id>`.
```

## 3. Live session status (hooks)

With Claude Code hooks the board shows whether a session is working, waiting for you, idle or
gone, without the agent's help: the card's state line and its dot, the session's last activity,
and where the session runs, which decides what the card's **Open** does
([§4](#4-watch-the-board)). `relaybay setup` writes the hooks into your user settings
(`~/.claude/settings.json`, which the CLI and the desktop app's local sessions both read). It only
writes when you ask:

```bash
relaybay setup                          # dry run: what it found, the exact diff, its hash
relaybay setup --apply --expect <hash>  # write exactly that plan (a backup of the file first)
relaybay setup --check [--json]         # server, MCP, hook endpoint, settings: ok / FAIL
relaybay setup --remove --dry-run       # preview taking relaybay's hooks out again
relaybay setup --remove --expect <hash> # do it (with a backup)
```

`--expect` makes the write refuse when the file (or the plan) changed after you looked at the dry
run. Without it, `--apply` and `--remove` still refuse a file that changes while they run, but not
one that changed between your dry run and the write. `--check --json` prints the results as JSON
and exits non-zero when an item fails.

The same plan, buttons (Apply, Remove, Run check) and check results are on the board under
**Settings → Integrations** (`/settings/integrations`). Apply and Remove ask for confirmation and
write only the plan shown on the page: if the file changed meanwhile, they refuse and show the new
plan.

What it writes, and what it never touches:

- One hook entry for each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `Notification`, `Stop` and `SessionEnd`, appended after the hooks you already have. An entry is relaybay's only
  when it is a command hook that runs `-m relaybay.hook_client`, `relaybay-hook` (or `relaybay hook`) with
  `--url <scheme>://<host>:<port>/hooks/claude-code`, or an http hook whose URL path is exactly
  `/hooks/claude-code` on a loopback address or relaybay's configured host. Your own hooks with a
  similar name or path are never claimed. Nothing else in the file is changed, reordered or
  removed, and the file keeps its indentation and line endings; a symlinked settings file is
  written through to its target. Running `--apply`
  again changes nothing; a new port, data folder or install updates only relaybay's entries, in
  place (moving from a source checkout to the desktop program never leaves two sets).
- Every changed file is copied first to `settings.json.relaybay-backup-<timestamp>`. A file that
  is not UTF-8, not valid JSON, or whose `hooks` are not in the documented shape, is left
  alone.
- The token is never written into the settings. **http hooks** (Claude Code calls relaybay
  directly) send `Authorization: Bearer ${RELAYBAY_TOKEN}` with `allowedEnvVars`, so
  `RELAYBAY_TOKEN` must be set in the environment Claude Code starts with (for the desktop app: a
  user environment variable). The **command hook** runs a small forwarder with the Python that runs
  relaybay (`<python> -I -m relaybay.hook_client --url … --token-file …`), which reads the token
  file itself; nothing has to be on `PATH`. From the installed desktop program it runs the bundled
  forwarder instead (`<install folder>/relaybay-hook --url … --token-file …`), which starts faster.
  Setup refuses to write that while the program runs from a mounted disk image, macOS's
  translocation, the temp or the Downloads folder: install it first.
- The command hook is the default: it needs no environment variable and is silent when
  relaybay is down. `--mode http` (or the Hook type select on the board) writes http hooks instead;
  the plan then lists what they need.
- Shell quoting: on macOS and Linux, paths with spaces, `$` or backticks are single-quoted. On
  Windows Claude Code runs the command through Git Bash or PowerShell; paths with spaces are
  double-quoted (a path with `$` or a backtick is refused: the plan shows an error), and PowerShell
  needs the interpreter path without spaces. `--check` runs the hook's interpreter once
  (`-I -c "import relaybay.hook_client"`) to prove it still works, or asks
  `relaybay-hook --version` and compares the version with its own.
- The MCP server is not written: `~/.claude.json` belongs to Claude Code. Setup checks whether a
  user-scope `relaybay` server is registered there and otherwise prints the `claude mcp add`
  command from step 1.

Hooks never get in the way when relaybay is not running. The command hook prints nothing and always
exits 0, so the session sees nothing: a tool call gets one 0.3 s try (5 s for a push, a merge
or a change on a stand, whose approval check reads git), an event that carries state
(session start and end, a prompt, the end of a turn, a todo change) one retry within 2 s, and then
waits in a spool that the next run sends once relaybay answers again. http hook entries use a
10 s timeout and failures do not block the session, but Claude Code shows a non-blocking "hook
error" notice while relaybay is down. Hook input over 256 KB (for example a large file write) is
dropped quietly with `204`.

The hooks report Claude Code's own session id, not the desktop `local_…` id. On `SessionStart`
relaybay tells the agent that id ("Your relaybay hook session: <id>"); an agent that passes it as
`hook_session` on its writes links its status exactly. Until then relaybay links a hook session to
the one desktop session bound in the project whose `repo_path` contains the working directory;
when several could match it does not guess and lists the session under Integrations →
"Unlinked hook sessions". In the Claude desktop app the hook also knows the desktop session id
(`CLAUDE_CODE_HOST_SESSION_ID`), so after `/clear` or a restart the new Claude Code id is linked to
the same desktop session by itself. `SessionEnd` unbinds the session from its tasks (the history is
kept), except after `/clear` or `/resume` and for desktop sessions. A session can also be bound from
the task dialog (**Bind**, next to Unbind).

**Comments from the board.** A comment written on a task reaches the agent of the bound session
through these hooks, only once the agent linked itself with `hook_session`: with its next prompt or
tool call (as added context), and `Stop` keeps the turn going (at most twice) until the agent calls
`comment_ack` or answers with `comment_add`. An idle session sees the comment with its next prompt.
To have a new comment wake an idle session, add the opt-in wake-up hook (command mode, Claude Code
2.1.309 or later):

```bash
relaybay setup --wake                          # dry run with the extra Stop hook
relaybay setup --apply --wake --expect <hash>  # write it
relaybay setup --apply --no-wake               # take only the wake-up hook out again
```

It is an `asyncRewake` command hook that runs in the background after every turn and waits up to 25
minutes for a comment (or your acceptance of its plan on the board); when one
arrives it exits 2 with it, which wakes the session.
On an older Claude Code (or one whose version setup cannot read) it also needs `--force-wake`: a
build that ignored `asyncRewake` would run the hook in the foreground and every turn would end only
after up to 30 minutes. If you see that, remove it with `--no-wake`. Details:
[MCP tools → Owner comments through hooks](mcp-tools.md#owner-comments-through-hooks).

**Plan reminders and the todo list.** An agent that linked its hook session is reminded of its
task's current plan step through the same hooks, and relaybay shows its Claude Code todo under that
step. The todo tools are off by default (Claude Code 2.1.309); to turn them on, add this to your
Claude Code settings (`~/.claude/settings.json`). `relaybay setup` does not write it; it is your
choice:

```json
{ "env": { "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1" } }
```

Details: [MCP tools → Plan](mcp-tools.md#plan).

**What a session gets at its start.** On startup, `/clear`, compaction and resume, the same hook
restores the picture without the agent having to ask. A session that linked itself and is bound to
an open task gets that task's context: fields and progress, plan and current step, decisions, linked
document sections, environments, rules, unread comment ids, relevant notes and what changed since it
last read (other bound tasks as one line each). Any other session started in a project's folder gets a
short project digest, 1500 characters by default (`session_context_project_chars`). Elsewhere nothing
is added. To turn it off, put this in relaybay's `config.toml` (or set
`RELAYBAY_SESSION_CONTEXT=off`):

```toml
session_context = "off"
```

Details: [MCP tools → Digests](mcp-tools.md#digests).

For tests or a second machine profile, `--claude-home <folder>` (and `relaybay serve
--claude-home <folder>` for the board page) points setup at another folder holding
`.claude/settings.json` and `.claude.json`.

## 4. Watch the board

Open the board from the desktop program's tray (Open board), or open the login link printed by
`relaybay serve` (or by `relaybay token`). Cards appear and move as sessions call the tools; no
reload needed. The everyday loop of a card and its session is in
[Work a card with a session](how-to/work-a-card.md).

**The state line.** The task dialog and the task page start with the card's state, derived from the
hooks and the card, never set by hand; the same word shows on the board card next to the session's
dot:

- **Waits for you**: an open question or approval request, a review in `review` or `test`, "waiting
  on" naming you, or the session asking for its user; each reason is listed under the word.
- **Working** or **Idle**: by the bound session's hook status, with its last activity.
- **Ended**: the session of the latest binding has ended.
- **No session**: nothing is bound.

**One Open.** The button in the state line follows where the session runs:

- a desktop session opens in the Claude desktop app (its link is also under the **Session** fold:
  if nothing opens, copy it and paste it into the app);
- an ended terminal session opens a new terminal window running `claude --resume <id>` in the
  folder the session started in. relaybay does this only when the board is opened on the machine
  itself (a phone or a board behind a reverse proxy is refused), once until the session starts or
  60 seconds pass;
- a live terminal session is not opened: the button is disabled with "running in a terminal". The
  board cannot find that window, and a second process on a live session is unsafe.

When Open is disabled or missing, the line says why (for example "runs in the desktop app; its
desktop id is not known yet"). While no session works on the card, **Copy the brief** gives the
text a new session needs.

**How relaybay knows where a session runs.** The command hook sends Claude Code's
`CLAUDE_CODE_ENTRYPOINT` with `SessionStart`: `claude-desktop…` is the desktop app, `cli` is a
terminal; a session with a desktop id (`local_…`) is a desktop one in any case. When the hook cannot
tell (an older hook client, an http hook, an IDE or SDK entrypoint), the session start asks the
session once to say it with its next relaybay write:

```text
task_patch(task="ACME-12", fields={"session_surface": "terminal"},
           session="<its name>", hook_session="<id>")
```

**Why a comment has not arrived.** Each owner comment shows `delivered` or `not delivered yet`
with its reason: no session bound; the session ended; the session has not linked its hooks yet
(it passed no `hook_session`); the session waits for its user, so the comment arrives with its next
tool call; the session is idle and the wake-up hook is not waiting
([§3](#3-live-session-status-hooks)). The Inbox lists comments waiting longer than an hour under
"Comments not delivered".

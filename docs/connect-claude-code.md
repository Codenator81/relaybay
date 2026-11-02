# Connect Claude Code

relaybay runs one MCP server for all your sessions. [Install it](getting-started/install.md) and
start it first:

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

After `relaybay token --rotate`, restart `relaybay serve` and update the token in the entry.

## 2. Tell the agent when to use it

Add a short section like this to the project's `CLAUDE.md` or `AGENTS.md` (replace the slug):

```markdown
## Task board (relaybay)

This project is `acme-shop` on the relaybay board (MCP server `relaybay`).

- At the start of a session, call `project_snapshot(project="acme-shop")`; its first line names
  the working rules (read them with `rules_get(project="acme-shop")`).
- Before starting work, find the task with `task_list` or create it with `task_create` (with a
  short `description`: what to do and why); then call `task_bind_session` with your session name.
- When the work moves on, call `task_set_stage` (plan → dev → review → test → live → done).
- Keep "waiting on" and "next step" current: pass `next_step` and `waiting_on` with every
  `task_set_stage` and `report_add` (or use `task_update`).
- At the end of every stage, call `report_add` with your final report. Start it with a line
  `state · size · gist`, e.g. `verified · M · checkout works`, and pass `progress`: the updated
  summary of everything done on the task so far.
- When you stop working on a task and nobody takes over, call `task_unbind_session`.
- Record decisions the user makes with `decision_add`.
- Pass `session` on every write: your own desktop session id (`local_…`) if you know it, with
  `session_title=<your session title>`; otherwise your session name. If the session start told
  you "Your relaybay hook session: <id>", also pass `hook_session=<id>`.
```

## 3. Live session status (hooks)

With Claude Code hooks the board shows whether a session is working, waiting for you, idle or
gone, without the agent's help: a dot on the card's "open in desktop" icon and a "last activity"
line in the task dialog. `relaybay setup` writes the hooks into your user settings
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
**Integrations** (link in the header). Apply and Remove ask for confirmation and write only the
plan shown on the page: if the file changed meanwhile, they refuse and show the new plan.

What it writes, and what it never touches:

- One hook entry for each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `Notification`, `Stop` and `SessionEnd`, appended after the hooks you already have. An entry is relaybay's only
  when it is a command hook that runs `-m relaybay.hook_client` (or `relaybay hook`) with
  `--url <scheme>://<host>:<port>/hooks/claude-code`, or an http hook whose URL path is exactly
  `/hooks/claude-code` on a loopback address or relaybay's configured host. Your own hooks with a
  similar name or path are never claimed. Nothing else in the file is changed, reordered or
  removed, and the file keeps its indentation and line endings; a symlinked settings file is
  written through to its target. Running `--apply`
  again changes nothing; a new port or data folder updates only relaybay's entries.
- Every changed file is copied first to `settings.json.relaybay-backup-<timestamp>`. A file that
  is not UTF-8, not valid JSON, or whose `hooks` are not in the documented shape, is left
  alone.
- The token is never written into the settings. **http hooks** (Claude Code calls relaybay
  directly) send `Authorization: Bearer ${RELAYBAY_TOKEN}` with `allowedEnvVars`, so
  `RELAYBAY_TOKEN` must be set in the environment Claude Code starts with (for the desktop app: a
  user environment variable). The **command hook** runs a small forwarder with the Python that runs
  relaybay (`<python> -I -m relaybay.hook_client --url … --token-file …`), which reads the token
  file itself; nothing has to be on `PATH`.
- The command hook is the default: it needs no environment variable and is silent when
  relaybay is down. `--mode http` (or the Hook type select on the board) writes http hooks instead;
  the plan then lists what they need.
- Shell quoting: on macOS and Linux, paths with spaces, `$` or backticks are single-quoted. On
  Windows Claude Code runs the command through Git Bash or PowerShell; paths with spaces are
  double-quoted (a Python path with `$` or a backtick is not supported there), and PowerShell
  needs the interpreter path without spaces. `--check` runs the hook's interpreter once
  (`-I -c "import relaybay.hook_client"`) to prove it still works.
- The MCP server is not written: `~/.claude.json` belongs to Claude Code. Setup checks whether a
  user-scope `relaybay` server is registered there and otherwise prints the `claude mcp add`
  command from step 1.

Hooks never get in the way when relaybay is not running. The command hook gives up after 0.3 s per
network step, prints nothing and always exits 0, so the session sees nothing. http hook entries use a
2 s timeout and failures do not block the session, but Claude Code shows a non-blocking "hook
error" notice while relaybay is down. Hook input over 256 KB (for example a large file write) is
dropped quietly with `204`.

The hooks report Claude Code's own session id, not the desktop `local_…` id. On `SessionStart`
relaybay tells the agent that id ("Your relaybay hook session: <id>"); an agent that passes it as
`hook_session` on its writes links its status exactly. Until then relaybay links a hook session to
the one desktop session bound in the project whose `repo_path` contains the working directory;
when several could match it does not guess and lists the session under Integrations →
"Unlinked hook sessions". `SessionEnd` unbinds the session from its tasks (the history is kept).

**Comments from the board.** A comment written on a task reaches the agent of the bound session
through these hooks, only once the agent linked itself with `hook_session`: with its next prompt or
tool call (as added context), and `Stop` keeps the turn going (at most twice) until the agent calls
`comment_ack` or answers with `comment_add`. An idle session sees the comment with its next prompt.
To have a new comment wake an idle session, add the opt-in wake-up hook (experimental, command mode,
not verified against a live Claude Code):

```bash
relaybay setup --wake --force-wake                          # dry run with the extra Stop hook
relaybay setup --apply --wake --force-wake --expect <hash>  # write it
relaybay setup --apply --no-wake                            # take only the wake-up hook out again
```

It is an `asyncRewake` command hook that runs in the background after every turn and waits up to 25
minutes for a comment; when one arrives it exits 2 with the comment, which wakes the session.
`--force-wake` is required because no Claude Code version is documented to support `asyncRewake`:
a build that ignored it would run the hook in the foreground and every turn would end only after up
to 30 minutes. If you see that, remove it with `--no-wake`; `relaybay doctor` marks the hook
`UNVERIFIED`. Details:
[MCP tools → Owner comments through hooks](mcp-tools.md#owner-comments-through-hooks).

For tests or a second machine profile, `--claude-home <folder>` (and `relaybay serve
--claude-home <folder>` for the board page) points setup at another folder holding
`.claude/settings.json` and `.claude.json`.

## 4. Watch the board

Open the login link printed by `relaybay serve` (or by `relaybay token`). Cards appear and move as
sessions call the tools; no reload needed. For a session bound by its desktop session id, the card's
"Open in desktop" button opens it in the Claude desktop app; if nothing opens, use "Copy link" and
paste the link into the app.

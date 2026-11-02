# Connect Claude Code

relaybay runs one MCP server for all your sessions. Start it first:

```bash
uv run relaybay serve
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

- At the start of a session, call `project_snapshot(project="acme-shop")`.
- Before starting work, find the task with `task_list` or create it with `task_create`;
  then call `task_bind_session` with your session name.
- When the work moves on, call `task_set_stage` (plan → dev → test → live → done).
- Keep "waiting on" and "next step" current with `task_update`.
- At the end of every stage, call `report_add` with your final report. Start it with a line
  `state · size · gist`, e.g. `verified · M · checkout works`.
- Record decisions the user makes with `decision_add`.
- Pass `session` on every write: your own desktop session id (`local_…`) if you know it, with
  `session_title=<your session title>`; otherwise your session name.
```

## 3. Watch the board

Open the login link printed by `relaybay serve` (or by `relaybay token`). Cards appear and move as
sessions call the tools; no reload needed. For a session bound by its desktop session id, the card's
"Open in desktop" button opens it in the Claude desktop app; if nothing opens, use "Copy link" and
paste the link into the app.

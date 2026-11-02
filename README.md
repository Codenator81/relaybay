# relaybay

A live board and MCP server for people who run many Claude Code sessions across many repositories.
Sessions report their work through MCP tools; you watch **projects → tasks → stages → reports** update
in the browser, on the desktop or on your phone.

> **Status: early.** relaybay is built for my own workflow first and changes quickly. Expect rough
> edges; issues and ideas are welcome.

- **Board** — projects with per-stage counts; per project, columns `plan → dev → test → live → done`
  with cards showing the start of the task's description, a "Done:" summary of its progress, the
  waiting-on person, the session working on the task (or "no session"), the branch and the last
  report. Click a card to open the task in a dialog over the live board (its own URL,
  `/p/<project>?task=<key>`, full screen on a phone): description and progress in full, reports,
  session history, "Open in desktop", unbinding the session and editing the task. Each task also has
  a page of its own (`/t/<key>`). Live updates over Server-Sent Events, catch-up after sleep or a
  dropped connection, a stage tab bar on narrow screens.
- **Done column and archive** — the `done` column shows tasks closed in the last
  `done_window_days` (default 7); older ones are archived and come back with "Show archived" or a
  search by key or title (`/p/<project>?archived=1`, `?q=…`). Done cards hide "waiting on" and the
  next step.
- **External tracker mark** — a task can carry its issue in any tracker (tracker name, issue key,
  optional link) and a "work log entered" mark. The card shows the issue as a badge at the top (a
  link when a URL is set, ✓ once logged). With `require_tracker_log = true` a done task without
  the mark shows "not logged" and stays out of the archive until it is marked.
- **MCP server** — Streamable HTTP at `/mcp` in the same process: create and update tasks, change
  stages, keep a task's description and progress summary current, add reports and decisions, bind
  and unbind sessions, get a markdown snapshot of a project. A plain session name never replaces a
  task's desktop session binding: it becomes that session's alias.
- **Local and private** — one process, SQLite, binds to `127.0.0.1`, token-protected, no telemetry,
  no outbound calls, no CDN.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Codenator81/relaybay.git
cd relaybay
uv run relaybay serve
```

The server prints a login link (`http://127.0.0.1:8765/login?token=…`). Open it once; the browser
keeps an HttpOnly cookie and the token disappears from the address bar. Opening the board without
it shows a sign-in page: paste the token there and you return to the page you asked for.

`relaybay token` prints the token again together with the command that connects Claude Code:

```bash
uv run relaybay token
```

## Connect Claude Code

```bash
claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

`user` scope adds relaybay once for all repositories. See
[docs/connect-claude-code.md](docs/connect-claude-code.md) for `.mcp.json` and a short instruction
snippet for your project's `CLAUDE.md` / `AGENTS.md` that tells the agent when to call the tools.
The tool reference is in [docs/mcp-tools.md](docs/mcp-tools.md).

## Configuration

Settings come from defaults, then `config.toml` in the data directory, then `RELAYBAY_*` environment
variables, then command-line flags.

| Setting | Default | Env | Flag |
|---|---|---|---|
| data directory | per-user data dir (`platformdirs`) | `RELAYBAY_HOME` | `--home` |
| bind address | `127.0.0.1` | `RELAYBAY_HOST` | `--host` |
| port | `8765` | `RELAYBAY_PORT` | `--port` |
| extra allowed Host names | none | `RELAYBAY_ALLOWED_HOSTS` (comma-separated) | — |
| desktop session link | `claude://claude.ai/epitaxy/{id}` | `RELAYBAY_DESKTOP_LINK_TEMPLATE` | — |
| days a done task stays in the done column | `7` | `RELAYBAY_DONE_WINDOW_DAYS` | — |
| keep done tasks without the tracker work log on the board | `false` | `RELAYBAY_REQUIRE_TRACKER_LOG` | — |

The data directory holds `relaybay.sqlite`, the `token` file and the optional `config.toml`:

```toml
port = 8765
allowed_hosts = ["my-laptop.lan"]   # needed only when you bind to a LAN address
desktop_link_template = "claude://claude.ai/epitaxy/{id}"  # {id} = desktop session id (local_…)
done_window_days = 7                # older done tasks are archived
require_tracker_log = false         # true: done tasks without the tracker work log stay and show "not logged"

[stages.labels]                     # display names; the stage keys stay plan/dev/test/live/done
dev = "in progress"
done = "shipped"

[reports.labels]                    # report badges: inspected/changed/verified/committed/pushed/blocked, S/M/L
verified = "checked"

[reports.hints]                     # badge tooltips
M = "medium change"
```

`relaybay token --rotate` creates a new token; restart the server and update the Claude Code entry.

### Troubleshooting: "token not valid"

Each data folder has its own token. `relaybay serve` prints the folder it uses (`Data folder: …`);
run `relaybay token` with the same `--home` (or `RELAYBAY_HOME`). `relaybay token` prints its folder
and warns when the server on the port uses a different token (another folder, or a rotation without
a restart). After `relaybay token --rotate`, restart the server.

## Roadmap

1. **Stage 1 (0.1.0)** — server, data model and event log, token security, MCP tools, minimal live
   board with editing and a phone layout. *(in progress)*
2. **Stage 2** — import of existing markdown task registries, Claude Code hooks for live session
   status, full task view and event feed, packaging (`uvx`, Docker).
3. **Stage 3** — starting and steering agents from relaybay.

The architecture is described in [docs/PLAN.md](docs/PLAN.md); decisions are recorded in
[docs/adr/](docs/adr/).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI coding tools: see [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE)

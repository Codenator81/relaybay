# relaybay

[![CI](https://github.com/Codenator81/relaybay/actions/workflows/ci.yml/badge.svg)](https://github.com/Codenator81/relaybay/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/relaybay)](https://pypi.org/project/relaybay/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Docs](https://github.com/Codenator81/relaybay/actions/workflows/docs.yml/badge.svg)](https://codenator81.github.io/relaybay/)

A live board and MCP server for people who run many Claude Code sessions across many repositories.
Sessions report their work through MCP tools; you watch **projects → tasks → stages → reports** update
in the browser, on the desktop or on your phone.

> **Status: early (0.1).** relaybay is built for my own workflow first and changes quickly. Expect
> rough edges; issues and ideas are welcome.

![The relaybay board updating live while sessions report their work](https://raw.githubusercontent.com/Codenator81/relaybay/main/docs/assets/board.gif)

## Quick start

```bash
uv tool install relaybay      # or: pipx install relaybay  ·  or run it once: uvx relaybay
relaybay serve                # prints the board's login link and the MCP URL
relaybay setup                # shows how it connects Claude Code (then --apply --expect <hash>)
```

Open the login link once; the browser keeps a cookie. `relaybay token` prints the token again with
the `claude mcp add …` command for Claude Code. Docker, Windows/macOS/Linux details and a procedure
your coding agent can follow for you:
[Install](https://codenator81.github.io/relaybay/getting-started/install/) ·
[Install with your coding agent](https://codenator81.github.io/relaybay/getting-started/agent-install/).
Something off? `relaybay doctor`.

**Documentation:** [codenator81.github.io/relaybay](https://codenator81.github.io/relaybay/) ·
[Specification](docs/SPEC.md) (how it behaves now) · [Roadmap](docs/ROADMAP.md) ·
[Architecture](docs/architecture.md)

## Features

- **Board** — projects with per-stage counts; per project, columns `plan → dev → test → live → done`
  with cards showing only the essentials: key, tracker badge, an "open in desktop" icon when a
  desktop session works on the task, title, waiting-on person, branch and the next step. Click a
  card to open the task in a dialog over the live board (its own URL,
  `/p/<project>?task=<key>`, full screen on a phone): description and progress in full, reports,
  session history, "Open in desktop", unbinding the session and editing the task. Each task also has
  a page of its own (`/t/<key>`). Live updates over Server-Sent Events, catch-up after sleep or a
  dropped connection, a stage tab bar on narrow screens.
- **Next step that keeps up** — sessions pass the next step and who acts next together with a
  stage change or a report; a next step left over from before the last stage change shows muted
  and marked "stale".
- **Done column and archive** — the `done` column shows tasks closed in the last
  `done_window_days` (default 7); older ones are archived and come back with "Show archived" or a
  search by key or title (`/p/<project>?archived=1`, `?q=…`). Done cards hide "waiting on" and the
  next step.
- **External tracker mark** — a task can carry its issue in any tracker (tracker name, issue key,
  optional link) and a "work log entered" mark. The card shows the issue as a badge at the top (a
  link when a URL is set, ✓ once logged). With `require_tracker_log = true` a done task without
  the mark shows "not logged" and stays out of the archive until it is marked. Every issue key a
  task has had is kept in its tracker history (task dialog, `/t/<key>`, `task_get`), and new work
  on a logged task (a report, a progress change, a move back from `live` or `done`) clears the
  mark again, so it always means "the log covers everything done".
- **MCP server** — Streamable HTTP at `/mcp` in the same process: create and update tasks, change
  stages, keep a task's description and progress summary current, add reports and decisions, bind
  and unbind sessions, get a markdown snapshot of a project. A plain session name never replaces a
  task's desktop session binding: it becomes that session's alias. `task_patch` takes any task
  fields as one JSON object, so clients with an older tool schema can still set new fields.
- **Working rules** — the server tells every connected session how to work with the board (MCP
  instructions: read the snapshot, pick a task, keep next step / waiting on / progress current,
  report as `state · size · gist`) and serves the rules as data: report format, what a stage move
  needs (by default a report before `test`, `progress` before `done`), a description template and
  free-text guidance. Global rules come from `config.toml`, a project can override them; agents read
  them through `rules_get` or `relaybay://rules/<project>`, and `project_snapshot` starts with the
  rules hash. A stage move that misses a rule succeeds with a warning (or is refused with
  `rules_mode = "strict"`), in MCP responses and on the board.
- **Local and private** — one process, SQLite, binds to `127.0.0.1`, token-protected, no telemetry,
  no outbound calls, no CDN.

## Try it with demo data

```bash
relaybay demo --home ./relaybay-demo                 # acme-shop and demo-api, tasks in every stage
relaybay serve --home ./relaybay-demo --port 8766
```

## Connect Claude Code

```bash
claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

`user` scope adds relaybay once for all repositories. See
[docs/connect-claude-code.md](docs/connect-claude-code.md) for `.mcp.json` and a short instruction
snippet for your project's `CLAUDE.md` / `AGENTS.md` that tells the agent when to call the tools.

### Live session status (hooks)

```bash
relaybay setup                         # what it found, the exact diff and its hash
relaybay setup --apply --expect <hash> # adds relaybay's hooks to ~/.claude/settings.json
relaybay setup --check                 # end-to-end check, one ok/FAIL line per item
```

The hooks let the board show whether each session is working, waiting for you, idle or gone, and
unbind a session from its task when it ends. Setup adds only its own entries (other hooks stay as
they are), never writes the token into the file, and `--remove` takes them out again. The same plan
and buttons are on the board under **Settings → Integrations**. Hooks stay silent when relaybay is not
running. Details: [docs/connect-claude-code.md](docs/connect-claude-code.md#3-live-session-status-hooks).

### Schema changes and long-running sessions

A Claude Code session keeps the parameter schema of a tool it already loaded, even after relaybay
restarts and the client reconnects: new tools appear, but new parameters of an existing tool do
not. Use `task_patch(task, fields={...})` for fields your client does not know yet (the allowed
names are at the end of `task_get` and in `relaybay://schema/task`), or refresh the schemas by
reconnecting the server in the client (`/mcp` in the CLI) or starting a new session. See
[docs/mcp-tools.md](docs/mcp-tools.md#schema-changes-and-long-running-sessions).
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
| what a stage move that misses a rule does: `warn` or `strict` | `warn` | `RELAYBAY_RULES_MODE` | — |
| `waiting_on` names that mean you (listed in the Inbox) | `owner`, `me`, `you` | `RELAYBAY_OWNER_NAMES` (comma-separated) | — |
| folder with Claude Code's `.claude/` and `.claude.json` (setup, Integrations) | your home directory | `RELAYBAY_CLAUDE_HOME` | `--claude-home` |

The data directory holds `relaybay.sqlite`, the `token` file and the optional `config.toml`:

```toml
port = 8765
allowed_hosts = ["my-laptop.lan"]   # needed only when you bind to a LAN address
desktop_link_template = "claude://claude.ai/epitaxy/{id}"  # {id} = desktop session id (local_…)
done_window_days = 7                # older done tasks are archived
require_tracker_log = false         # true: done tasks without the tracker work log stay and show "not logged"
owner_names = ["owner", "me"]       # waiting_on values listed in the Inbox
rules_mode = "warn"                 # "strict": a stage move that misses a rule is refused

[stages.labels]                     # display names; the stage keys stay plan/dev/test/live/done
dev = "in progress"
done = "shipped"

[reports.labels]                    # report badges: inspected/changed/verified/committed/pushed/blocked, S/M/L
verified = "checked"

[reports.hints]                     # badge tooltips
M = "medium change"

[rules]                             # global working rules, merged over the built-in defaults
guidance = "Ask the owner before anything goes live."
description_template = "Goal:\nWhy:\nDone when:"

[rules.report_format]               # states/sizes: a subset of the built-in ones
sizes = ["S", "M", "L"]

[rules.stage_requirements]          # per target stage; a list replaces the default, [] drops it
test = [{ kind = "report_since_stage_change" }]
done = [{ kind = "field_present", field = "progress" }]
```

A rules error in `config.toml` (unknown key, stage, rule kind or field) stops `relaybay serve`
with a message that names the key. A broken project override never stops the server: it is
logged, the project falls back to the global rules, and `rules_get` and `project_snapshot` say
"project override ignored: <error>". Project overrides and the merge rules are described in
[docs/mcp-tools.md](docs/mcp-tools.md#working-rules).

`relaybay token --rotate` creates a new token; restart the server and update the Claude Code entry.

### Troubleshooting: "token not valid"

Each data folder has its own token. `relaybay serve` prints the folder it uses (`Data folder: …`);
run `relaybay token` with the same `--home` (or `RELAYBAY_HOME`). `relaybay token` prints its folder
and warns when the server on the port uses a different token (another folder, or a rotation without
a restart). After `relaybay token --rotate`, restart the server. `relaybay doctor` checks this and
everything else in one command ([troubleshooting](https://codenator81.github.io/relaybay/how-to/troubleshooting/)).

## Roadmap

- **0.1.0** — board, MCP tools, working rules, hooks and `relaybay setup`, packaging
  (`uv tool install`, `uvx`, `pipx`, Docker), `relaybay doctor`, docs site, demo workspace.
- **0.2.0** — sidebar navigation and Inbox, comments to agents, project environments and access.
- **0.3.0** — review checklist with screenshots, memory and documents.
- **Later** — annotations, map and usage, rules versioning, intake, agents started from relaybay.

Details and status: [docs/ROADMAP.md](docs/ROADMAP.md). Designs of upcoming steps are in
[docs/design/](docs/design/index.md), decisions in [docs/adr/](docs/adr/). relaybay sends no
telemetry.

## Contributing and feedback

- Questions, ideas, show and tell: [Discussions](https://github.com/Codenator81/relaybay/discussions).
- Bugs and feature requests: [issues](https://github.com/Codenator81/relaybay/issues/new/choose)
  (include `relaybay doctor` output for bugs).
- Security problems: report privately, see [SECURITY.md](SECURITY.md).
- Code: [CONTRIBUTING.md](CONTRIBUTING.md); AI coding tools: [AGENTS.md](AGENTS.md). Everyone
  follows the [Code of Conduct](CODE_OF_CONDUCT.md); how decisions are made:
  [GOVERNANCE.md](GOVERNANCE.md).

## License

[MIT](LICENSE)

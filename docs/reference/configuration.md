# Configuration reference

Settings come from defaults, then `config.toml` in the data directory, then `RELAYBAY_*` environment
variables, then command-line flags (a later source wins). An invalid value stops `relaybay serve`
with a message that names the key.

!!! note
    In 0.1.0 this page will be generated from the settings schema, and CI will fail when it drifts.

## Settings

| Key (`config.toml`) | Default | Environment | Flag | Meaning |
|---|---|---|---|---|
| — | per-user data dir (`platformdirs`) | `RELAYBAY_HOME` | `--home` | Data directory: `relaybay.sqlite`, `token`, `config.toml`. |
| `host` | `127.0.0.1` | `RELAYBAY_HOST` | `--host` | Bind address. Anything but loopback prints a warning. |
| `port` | `8765` | `RELAYBAY_PORT` | `--port` | Port. |
| `allowed_hosts` | `[]` | `RELAYBAY_ALLOWED_HOSTS` (comma-separated) | — | Extra names accepted in the `Host` header (LAN names). |
| `desktop_link_template` | `claude://claude.ai/epitaxy/{id}` | `RELAYBAY_DESKTOP_LINK_TEMPLATE` | — | Link for a desktop session id; must contain `{id}`. |
| `done_window_days` | `7` | `RELAYBAY_DONE_WINDOW_DAYS` | — | Days a done task stays in the done column (whole number ≥ 1). |
| `require_tracker_log` | `false` | `RELAYBAY_REQUIRE_TRACKER_LOG` | — | Done tasks without the tracker mark show "not logged" and are never archived. |
| `rules_mode` | `warn` | `RELAYBAY_RULES_MODE` | — | `warn` or `strict`: what a stage move that misses a rule does. |
| `claude_home` | your home directory | `RELAYBAY_CLAUDE_HOME` | `--claude-home` | Folder with Claude Code's `.claude/` and `.claude.json` (setup, Integrations). |
| `[stages.labels]` | the stage keys | — | — | Display names per stage (`plan`, `dev`, `test`, `live`, `done`). |
| `[reports.labels]` | built-in | — | — | Display names of report states and sizes. |
| `[reports.hints]` | built-in | — | — | Tooltips of report badges. |
| `[rules]` | built-in rules | — | — | Global working rules merged over the defaults ([SPEC §6](../SPEC.md#spec-rules)). |

## Example `config.toml`

```toml
port = 8765
allowed_hosts = ["my-laptop.lan"]   # needed only when you bind to a LAN address
desktop_link_template = "claude://claude.ai/epitaxy/{id}"
done_window_days = 7
require_tracker_log = false
rules_mode = "warn"

[stages.labels]
dev = "in progress"
done = "shipped"

[reports.labels]
verified = "checked"

[reports.hints]
M = "medium change"

[rules]
guidance = "Ask the owner before anything goes live."
description_template = "Goal:\nWhy:\nDone when:"

[rules.report_format]
sizes = ["S", "M", "L"]

[rules.stage_requirements]
test = [{ kind = "report_since_stage_change" }]
done = [{ kind = "field_present", field = "progress" }]
```

# Troubleshooting

## Start with `relaybay doctor`

```bash
relaybay doctor          # one ok / FAIL line per item
relaybay doctor --json   # the same as a JSON list of {"ok", "name", "detail"}
```

It reads, never writes: relaybay, Python and OS versions; the data folder and whether it is
writable; the token file (present, private); Claude Code on `PATH` and whether it is at least the
minimum supported version; the desktop app; the server on the configured port with the same token,
MCP `initialize` and the hook endpoint; the rules hash of your settings against the one the server
started with; relaybay's hooks in the Claude Code user settings and the interpreter they run; the
MCP registration. The exit code is `1` when an item fails. Pass the same `--home`, `--port` and
`--claude-home` as the server and setup use.

| Failing item | Usual cause and fix |
|---|---|
| `token file` missing | The server never ran with this data folder: start `relaybay serve` (with the same `--home`). |
| `relaybay server` … another token | The server runs with another data folder, or the token was rotated without a restart. |
| `rules hash` … restart | `config.toml` changed after the server started: restart `relaybay serve`. |
| `Claude Code` | Not on `PATH`, or older than the minimum supported version: update Claude Code. |
| `hooks in user settings` | Run `relaybay setup`, then `relaybay setup --apply --expect <hash>`. |
| `command hook runs` | The Python the hooks point at moved (venv deleted, upgrade), or the `relaybay-hook` they start belongs to another install or version: run `relaybay setup --apply` again with the `relaybay` you use (the desktop program: Settings → Reconnect Claude Code). |
| `install` | The desktop program runs from a mounted disk image, macOS's translocation, the temp or the Downloads folder: install it (macOS: move relaybay.app to Applications) and start it from there. |
| `hook executable` | The installed program's `relaybay-hook` is missing or answers another version: reinstall the program. |
| `MCP registered for Claude Code` | Run the `claude mcp add …` line that `relaybay token` prints. |

## "token not valid"

Each data folder has its own token. `relaybay serve` prints the folder it uses (`Data folder: …`);
run `relaybay token` with the same `--home` (or `RELAYBAY_HOME`). `relaybay token` prints its folder
and warns when the server on the port uses a different token (another folder, or a rotation without
a restart). After `relaybay token --rotate`, restart the server and update the Claude Code entry.

## The board does not show session status

Run the end-to-end check:

```bash
relaybay setup --check
```

Each line is `ok` or `FAIL`: server reachable with the same token, MCP with the token, hook endpoint,
hooks in the user settings, the hook's interpreter, MCP registration. A hook session that relaybay
could not match to a card unambiguously is listed under **Integrations → Unlinked hook sessions**;
the agent links itself by passing `hook_session` on its next write.

## A new task field is "unknown" in a running session

The client keeps the parameter schema of a tool it already loaded. Use
`task_patch(task, fields={...})`, or reconnect the server (`/mcp` in the CLI) or start a new session.
See [MCP tools](../mcp-tools.md#schema-changes-and-long-running-sessions).

## `relaybay serve` stops with a config error

An invalid value in `config.toml` or a `RELAYBAY_*` variable stops the start with a message that
names the key. See the [configuration reference](../reference/configuration.md).

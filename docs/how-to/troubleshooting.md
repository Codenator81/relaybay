# Troubleshooting

!!! note
    In 0.1.0 `relaybay doctor --json` will check all of this in one command.

## "token not valid"

Each data folder has its own token. `relaybay serve` prints the folder it uses (`Data folder: …`);
run `relaybay token` with the same `--home` (or `RELAYBAY_HOME`). `relaybay token` prints its folder
and warns when the server on the port uses a different token (another folder, or a rotation without
a restart). After `relaybay token --rotate`, restart the server and update the Claude Code entry.

## The board does not show session status

Run the end-to-end check:

```bash
uv run relaybay setup --check
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

# CLI reference

!!! note
    In 0.1.0 this page will be generated from the argument parser, and CI will fail when it drifts.

`relaybay [--version] <command>`; without a command, `serve` runs. `python -m relaybay` is the same.

| Command | Options | What it does |
|---|---|---|
| `serve` | `--host`, `--port`, `--home`, `--claude-home`, `--reload` | Starts the server; prints the version, the data folder, the board login link and the MCP URL. |
| `token` | `--home`, `--rotate` | Prints the token, the data folder, the `claude mcp add` command and the login link; warns when the server on the port uses a different token. |
| `setup` | `--dry-run` (default), `--apply`, `--remove`, `--check`, `--expect <hash>`, `--json`, `--mode command\|http`, `--home`, `--port`, `--claude-home` | Connects Claude Code on this machine ([SPEC §5](../SPEC.md#spec-setup)). |
| `hook` | `--home`, `--port` | Forwards one hook event from stdin to the server; always exits 0. |

Exit codes: `0` success; `1` a failed setup write or check; `2` invalid settings or arguments.

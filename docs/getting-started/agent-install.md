# Install relaybay with your coding agent

This page is written for a coding agent (Claude Code or any other) that installs and connects
relaybay for its user. Every step is safe to run again: it checks first and changes only what is
missing. The only file outside relaybay's own data folder that gets written is the user's Claude
Code settings, and only after the user has seen and approved the exact change.

## Prompt for the user

Paste this into your coding agent:

```text
Install and connect relaybay on this machine following
https://codenator81.github.io/relaybay/getting-started/agent-install/
Show me the setup plan and wait for my OK before you apply it.
```

## Procedure for the agent

Follow the steps in order. Stop and report to the user when a step fails; do not work around a
failure (no editing of `~/.claude/settings.json` or `~/.claude.json` by hand).

### 1. Detect the system

- OS: Windows, macOS or Linux (`uname -s`, or `$env:OS` / `[System.Environment]::OSVersion` in
  PowerShell). On Windows use PowerShell syntax; there is no `&&` in Windows PowerShell 5.1.
- Is relaybay already installed? `relaybay --version`. If it prints `relaybay <version>`, skip
  step 2.
- Is it the desktop program? The installed program's `relaybay` answers `--version` too, so run
  `relaybay doctor` and read two items: `install` (`desktop-windows`, `desktop-macos` or
  `desktop-deb` means the desktop program) and `server mode` (`embedded` or `external`). If the
  desktop program is installed, use it: step 3 starts the program, steps 4 to 7 (`relaybay token`,
  `relaybay setup`) run with its `relaybay`; never start a bare `relaybay serve` next to it.
- Is `uv` available? `uv --version`. Otherwise `pipx --version`.
- Is Claude Code available? `claude --version`. relaybay works without it, but steps 5–6 need it.

### 2. Install

Use the first tool that is available; ask the user before installing `uv` itself.

```bash
uv tool install relaybay        # or: pipx install relaybay
relaybay --version
```

If `relaybay` is not found afterwards, the tool's bin folder is not on `PATH`: run
`uv tool update-shell` (or `pipx ensurepath`) and tell the user to open a new terminal.

### 3. Start the server in the background

**Desktop program installed** (step 1): do not run `relaybay serve`. A server started that way
bypasses the program: the program then finds the port taken and runs no server of its own. Start
the program instead (it runs its server inside):

- Windows: the Start Menu entry relaybay, or
  `Start-Process "$env:LOCALAPPDATA\Programs\relaybay\relaybay-desktop.exe"`
- macOS: `open -a relaybay`
- Linux: the relaybay entry in the applications menu, or `/opt/relaybay/relaybay-desktop &`

With `server mode` `embedded`, wait until `curl -s http://127.0.0.1:8765/healthz` answers (up to
10 seconds), then go to step 4. With `server mode` `external` the server runs elsewhere (for example in Docker): start no server;
the program and the CLI (`token`, `setup`, `doctor`) talk to `server_url` from `desktop.toml`. If
`relaybay doctor` reports that server unreachable, tell the user; otherwise go to step 4.

**No desktop program:** check first; start only when nothing answers:

```bash
curl -s http://127.0.0.1:8765/healthz
```

A JSON answer with `"status": "ok"` means a server is already running: go to step 4. Otherwise
start `relaybay serve` as a background process that outlives your shell command, for example:

- macOS / Linux: `nohup relaybay serve > ~/relaybay.log 2>&1 &`
- Windows (PowerShell): `Start-Process relaybay -ArgumentList serve -WindowStyle Hidden`

Wait until `/healthz` answers (up to 10 seconds). Never print the token into the chat; the user
opens the board with the link from `relaybay token`, which they run themselves.

### 4. Register the MCP server

`relaybay token` prints a ready `claude mcp add --transport http --scope user relaybay …` command
with the token filled in. Run that line exactly as printed, without echoing it back to the user.
Skip this step when `claude mcp list` already shows `relaybay`; if it shows `relaybay` with
another URL, ask the user before replacing it (`claude mcp remove relaybay --scope user`, then
add).

### 5. Show the hook plan

```bash
relaybay setup --dry-run
```

It prints what it detected, the exact diff of `~/.claude/settings.json` and a plan hash. Show the
diff to the user and explain: it adds relaybay's hook entries (live session status on the board),
keeps every other setting and hook, writes a backup first and never writes the token. If it says
"already set up, no change", go to step 7.

### 6. Apply only after the user agrees

When the user says yes, apply exactly the plan they saw:

```bash
relaybay setup --apply --expect <plan hash>
```

If it refuses because the plan changed, run step 5 again and ask again. If the user says no, stop
here; MCP alone already works.

### 7. Verify

```bash
relaybay setup --check --json
```

A JSON list of `{"ok", "name", "detail"}` items; the exit code is `0` when every item is ok. If
anything fails, run:

```bash
relaybay doctor --json
```

and report the failing items with their `detail` to the user. Common causes: the server runs with
another data folder (another token), the server needs a restart after `config.toml` changed, or
Claude Code is older than the minimum supported version.

### 8. Tell the user what is next

- Open the board: run `relaybay token` and open the "Open the board" link once.
- New Claude Code sessions see the `relaybay` MCP server; running sessions need `/mcp` to
  reconnect or a restart.
- Optional: add the snippet from
  [Connect Claude Code](../connect-claude-code.md#2-tell-the-agent-when-to-use-it) to the
  project's `CLAUDE.md` or `AGENTS.md`.

## Undo

`relaybay setup --remove` (with `--dry-run` first) takes relaybay's hooks out again,
`claude mcp remove relaybay --scope user` removes the MCP entry. See
[Remove the integration](../how-to/uninstall.md).

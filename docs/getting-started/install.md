# Install and run

relaybay is a Python package (Python 3.12+) with one command, `relaybay`. It runs on Windows, macOS
and Linux. Pick one way to install it; [uv](https://docs.astral.sh/uv/) is the easiest, because it
also brings a suitable Python.

## 1. Install

=== "uv (recommended)"

    Install uv once ([instructions](https://docs.astral.sh/uv/getting-started/installation/)),
    then:

    ```bash
    uv tool install relaybay
    relaybay --version
    ```

    `uv tool upgrade relaybay` updates it; `uv tool uninstall relaybay` removes it.

=== "uvx (no install)"

    Run the latest release without installing it:

    ```bash
    uvx relaybay serve
    ```

    Every `relaybay …` command on these pages works as `uvx relaybay …`. For the Claude Code hooks
    an installed copy is better: the hook command points at the Python that ran
    `relaybay setup`, and uvx's cached environments can move.

=== "pipx"

    ```bash
    pipx install relaybay
    relaybay --version
    ```

=== "Docker"

    ```bash
    docker run -d --name relaybay --restart unless-stopped \
      -p 127.0.0.1:8765:8765 -v relaybay-data:/data ghcr.io/codenator81/relaybay:latest
    docker logs relaybay          # the login link
    docker exec relaybay relaybay token
    ```

    Inside the container relaybay listens on `0.0.0.0` (and prints a warning about it); publish the
    port **only on `127.0.0.1`** as above, so nothing outside your machine reaches it. Requests
    from the host arrive with `Host: 127.0.0.1:8765`, which the Host check accepts. Data, the token
    and `config.toml` live in the `/data` volume.

    The hooks run on the host, not in the container: install relaybay on the host as well for the
    default command hooks, or use `relaybay setup --mode http` with `RELAYBAY_TOKEN` set for Claude
    Code ([details](../connect-claude-code.md#3-live-session-status-hooks)).

On Windows, run the commands in PowerShell or Windows Terminal; nothing else differs.

## 2. First run and the token

```bash
relaybay serve
```

The server prints its version, its data folder, a login link
(`http://127.0.0.1:8765/login?token=…`) and the MCP URL. Open the link once: the browser keeps an
HttpOnly cookie and the token disappears from the address bar. Opening the board without it shows a
sign-in page where you can paste the token.

The data folder is your per-user data directory (for example `~/.local/share/relaybay` on Linux,
`~/Library/Application Support/relaybay` on macOS, `%LOCALAPPDATA%\relaybay` on Windows), or the
one you pass with `--home` / `RELAYBAY_HOME`. It holds `relaybay.sqlite`, the `token` file, the
optional `config.toml` and `backups/`: before a new version migrates the database, relaybay copies
it there with the settings files and keeps the newest five.

`relaybay token` prints the token again with the command that connects Claude Code:

```bash
relaybay token
```

To keep the server running, start it in the background the way your OS does it (a terminal tab, a
login item, `systemd --user`, a scheduled task) or use the Docker image with `--restart`.

## 3. Connect Claude Code

```bash
relaybay token     # copy the "claude mcp add …" line it prints and run it
relaybay setup     # shows what it would add to ~/.claude/settings.json, and the plan hash
relaybay setup --apply --expect <hash>
relaybay setup --check
```

`relaybay setup` adds only relaybay's own hook entries (with a backup) and never writes the token
into the file. Details: [Connect Claude Code](../connect-claude-code.md). Prefer to let your agent
do it? See [Install with your coding agent](agent-install.md).

## 4. The first task from a session

Open Claude Code in any repository and ask it, for example:

> Add this repository to relaybay as project `acme-shop` and create a task "Try relaybay" in plan.

The session calls `project_upsert` and `task_create`; the card appears on the board at once,
without a reload. Add the short snippet from
[Connect Claude Code](../connect-claude-code.md#2-tell-the-agent-when-to-use-it) to the project's
`CLAUDE.md` or `AGENTS.md`, so every session keeps its task current.

## Try it with demo data

```bash
relaybay demo --home ./relaybay-demo
relaybay serve --home ./relaybay-demo --port 8766
```

`relaybay demo` fills a separate data folder with two made-up projects (`acme-shop`, `demo-api`)
and tasks in every stage. It refuses a folder that already holds a board (`--force` renames that
database aside first).

## If something is off

```bash
relaybay doctor
```

One `ok` / `FAIL` line per item: versions, data folder and token, Claude Code, the server, hooks
and the MCP registration. See [Troubleshooting](../how-to/troubleshooting.md).

## Phone and LAN

The board works on a phone. relaybay binds to `127.0.0.1` by default; to reach it from another
device, see [Phone and LAN access](../how-to/lan-access.md).

## From a checkout

To run the development version: `git clone https://github.com/Codenator81/relaybay.git`, then
`uv run relaybay serve` in the folder. See
[CONTRIBUTING.md](https://github.com/Codenator81/relaybay/blob/main/CONTRIBUTING.md).

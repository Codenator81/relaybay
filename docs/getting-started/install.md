# Install and run

relaybay runs on Windows, macOS and Linux, as a desktop program from an installer or as a Python
package (Python 3.12+) with one command, `relaybay`. Pick one way to install it: the desktop
program if you want a tray icon and a window without a terminal, [uv](https://docs.astral.sh/uv/)
if you live in one (it also brings a suitable Python).

## 1. Install

=== "Desktop program"

    Download the installer for your system from the
    [latest release](https://github.com/Codenator81/relaybay/releases/latest). Each release also
    has `SHA256SUMS.txt` to check the download. The installers are not signed with a certificate,
    so each OS warns the first time; the steps below get past it. macOS and Linux builds are
    **beta**: CI tests them, but they get less use than the Windows one.

    **Windows (x64).** Run `relaybay-<version>-windows-x64-setup.exe`. It installs for your user
    only, without admin rights, into `%LOCALAPPDATA%\Programs\relaybay`, with a Start Menu entry
    and two options: start relaybay at login (off) and add `relaybay` to your `PATH` (on).
    SmartScreen shows "Windows protected your PC": click **More info**, then **Run anyway**.
    A silent install for scripts: `relaybay-<version>-windows-x64-setup.exe /VERYSILENT`.

    **macOS (Apple silicon, beta).** Open `relaybay-<version>-macos-arm64.dmg` and drag
    relaybay to **Applications**; start it from there, not from the disk image (setup refuses a
    program that runs from the image). The app is not notarized, so the first start is blocked.
    On macOS 15 the right-click **Open** bypass is gone: open System Settings → Privacy & Security,
    scroll to the message about relaybay and click **Open Anyway**. Or, in a terminal:

    ```bash
    xattr -dr com.apple.quarantine /Applications/relaybay.app
    ```

    The command line is inside the bundle:
    `/Applications/relaybay.app/Contents/MacOS/relaybay`.

    **Linux (amd64, `.deb`, beta).** Ubuntu 22.04 or newer, Debian 12 or newer:

    ```bash
    sudo apt install ./relaybay_<version>_amd64.deb
    ```

    It installs into `/opt/relaybay` with `relaybay` in `/usr/bin`. You get the tray icon and the
    board in your browser; a board window on Linux comes later. GNOME shows tray icons only with
    the AppIndicator extension.

    The program runs the same server as `relaybay serve` on `127.0.0.1:8765`, so a `relaybay serve`
    you run yourself must be stopped first (the program says so when the port is taken). Its tray
    menu has Open board, Restart server, Logs, Settings and Quit; closing the window keeps it
    running in the tray. Updates: the tray offers **Update to X.Y.Z** when a release is out.

    Moving from a server you ran from a source checkout? See
    [Move to the desktop program](../how-to/desktop-from-source-server.md). A server in Docker
    with the program as its client:
    [Docker server, desktop client](../how-to/docker-server-with-desktop-client.md).

=== "uv"

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
one you pass with `--home` / `RELAYBAY_HOME`, or the desktop program's data folder setting
(`data_dir` in `desktop.toml`, which the command line reads too). It holds `relaybay.sqlite`, the `token` file, the
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
into the file. Run it with the `relaybay` of the install you use: from the desktop program, the
hooks start its `relaybay-hook` (for example
`%LOCALAPPDATA%\Programs\relaybay\relaybay-hook.exe`); from a uv tool or pipx, that Python. The
program also has **Settings → Reconnect Claude Code**, which shows the same plan and diff and
writes it when you click Apply, and it tells you in the tray when the hooks point somewhere else. Details: [Connect Claude Code](../connect-claude-code.md). Prefer to let your agent
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

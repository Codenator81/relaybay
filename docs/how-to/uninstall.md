# Remove the integration and uninstall

## 1. Take relaybay out of Claude Code

Do this first, with the `relaybay` you are about to remove: afterwards the hooks would call a
program that is gone (they stay silent, but they stay).

Take relaybay's hooks out of your Claude Code user settings (only relaybay's own entries; a backup
of the file is written first):

```bash
relaybay setup --remove --dry-run        # preview the removal and its hash
relaybay setup --remove --expect <hash>  # write exactly that plan
```

The same is the **Remove** button under **Integrations** on the board.

Remove the MCP server from Claude Code:

```bash
claude mcp remove relaybay --scope user
```

## 2. Uninstall the program

=== "Windows"

    Settings → Apps → Installed apps → relaybay → **Uninstall** (or Control Panel → Programs).
    The uninstaller asks a running program to quit, removes the install folder
    (`%LOCALAPPDATA%\Programs\relaybay`), the Start Menu entry, its `PATH` entry and the start-at-login
    entry. A `relaybay serve` you started yourself from the installed `relaybay` keeps its files
    open: stop it first.

=== "macOS"

    Quit relaybay (tray → Quit relaybay), then move `/Applications/relaybay.app` to the Trash. If
    start at login was on, turn it off first in Settings, or delete
    `~/Library/LaunchAgents/io.github.codenator81.relaybay.plist`.

=== "Linux (.deb)"

    Quit relaybay (tray → Quit relaybay), then:

    ```bash
    sudo apt remove relaybay
    rm -f ~/.config/autostart/relaybay.desktop    # if start at login was on
    ```

=== "uv, pipx, Docker"

    ```bash
    uv tool uninstall relaybay        # or: pipx uninstall relaybay
    docker rm -f relaybay             # the container; the volume stays (docker volume rm relaybay-data)
    ```

    Started with `examples/compose.yaml`:

    ```bash
    docker compose -f examples/compose.yaml down      # the container; the data volume stays
    ```

    `down -v` also deletes the `relaybay-data` volume, which **is the board**: the database, the
    token and the backups. Use it only when you mean to delete the data, after a copy if in doubt.

## 3. What stays

Uninstalling never deletes your data. The data folder (`relaybay.sqlite`, `token`, `config.toml`,
`backups/`, `logs/`) is printed by `relaybay serve` and `relaybay doctor`; delete it to remove the
board. The desktop program's settings (`desktop.toml`, `external-token`, `window-state.json`) are in your per-user config
folder (`relaybay doctor` shows the path in its `desktop` line); a reinstall picks them up again,
start at login included. Backups of the Claude Code settings file are next to it as
`settings.json.relaybay-backup-<timestamp>`.

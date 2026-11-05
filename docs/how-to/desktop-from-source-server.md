# Move from a source-checkout server to the desktop program

You run `relaybay serve` from a git checkout (for example a worktree at `~/relaybay-src`, started
with `uv run relaybay serve --home ~/relaybay-data`) and the Claude Code hooks point at that
checkout's Python. These steps switch to the installed desktop program and keep your board: the
program serves the same data folder, nothing is moved or converted by hand.

The examples use `~/relaybay-src` for the checkout and `~/relaybay-data` for the data folder;
use your own paths. On Windows, in PowerShell, write `$HOME\relaybay-data` instead of
`~/relaybay-data`: PowerShell does not expand `~` in the arguments of a program, and not every
`relaybay` option expands it itself (`--home` of `serve`, `setup` and `doctor` does not). Each step
shows both.

## 1. Find the data folder and stop the old server

```bash
cd ~/relaybay-src
uv run relaybay doctor --home ~/relaybay-data     # "data folder" names it, "token file" too
```

```powershell
cd $HOME\relaybay-src
uv run relaybay doctor --home $HOME\relaybay-data
```

Stop the old server (Ctrl+C in its terminal, or the service, scheduled task or login item that
starts it) and turn off whatever starts it at login, so the two never run at once: both want port
8765.

## 2. Back up the data folder

Copy the whole folder while no server runs, so the database files are consistent:

```bash
cp -a ~/relaybay-data ~/relaybay-data.before-desktop
```

```powershell
Copy-Item -Recurse $HOME\relaybay-data $HOME\relaybay-data.before-desktop
```

A newer version also copies the database into `backups/` of the data folder by itself the first
time it starts; your own copy covers everything else (attachments included).

## 3. Install the program and point it at the folder

Install the program for your system ([Install](../getting-started/install.md), "Desktop program"),
but do not start it yet (on Windows, clear "Start relaybay" on the last page of the setup): started
now, it would open a new, empty board in the default folder. Point the program's data folder
setting at your folder with the installed command line:

```bash
relaybay desktop config --home ~/relaybay-data
```

```powershell
relaybay desktop config --home $HOME\relaybay-data
```

This writes `data_dir` into `desktop.toml` (in your per-user config folder); the program and every
`relaybay` command read it, so `setup`, `doctor` and `token` find the same board. It moves nothing.
If you already started the program, use Settings → Data folder instead and save.

On macOS the installed command line is `/Applications/relaybay.app/Contents/MacOS/relaybay`; on
Windows the installer adds `relaybay` to your `PATH` (open a new terminal first). Check that you
run the installed one, not the checkout's: `relaybay doctor` shows `desktop-windows`,
`desktop-macos` or `desktop-deb` in its first line.

## 4. Re-run setup so the hooks use `relaybay-hook`

```bash
relaybay setup                           # the plan: your checkout's Python is replaced by relaybay-hook
relaybay setup --apply --expect <hash>   # write exactly that plan (with a backup)
```

relaybay's own entries are replaced in place, never added twice; your other hooks stay as they
are. The same plan is in the program under Settings → **Reconnect Claude Code**. An opt-in wake-up
hook, if you had one, is kept and moved to `relaybay-hook` too. The MCP registration does not
change: the URL and the token are the same.

## 5. Start the program and check

Start relaybay from the Start Menu, Applications or your desktop's app menu. The tray icon appears
and the board opens with your projects. Then:

```bash
relaybay doctor
```

Every line should be `ok`: `data folder source` says `data_dir in desktop.toml`, `hook executable`
names the installed `relaybay-hook` with the same version, and `command hook runs` checks the
hooks. Open a Claude Code session: it shows up on the board.

## Roll back

Quit the program (tray → Quit relaybay) and start the old server again from the checkout, as you
did before. The data folder is the same one, so the board is untouched. The hooks keep working as
long as the program stays installed (they call its `relaybay-hook` with the same URL and token); to
point them back at the checkout, run `uv run relaybay setup --apply` there. If the program is a
newer version than the checkout, it migrated the database: put back the copy from step 2 before you
start the older server.

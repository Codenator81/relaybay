# Troubleshooting

## Start with `relaybay doctor`

```bash
relaybay doctor          # one ok / FAIL line per item
relaybay doctor --json   # the same as a JSON list of {"ok", "name", "detail"}
```

It reads, never writes: relaybay, Python and OS versions; the data folder and whether it is
writable; the token file (present, private); Claude Code on `PATH` and whether it is at least the
minimum supported version; the desktop app; relaybay's own program (start at login, its last
start); hook events waiting in the spool; the server on the configured port with the same token,
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
| `hook timeout` | The hook entries come from an older `relaybay setup` with a shorter timeout, which cuts the approval check of a push short: run `relaybay setup`, then `relaybay setup --apply --expect <hash>` (the desktop program: Settings → Reconnect). |
| `command hook runs` | The Python the hooks point at moved (venv deleted, upgrade), or the `relaybay-hook` they start belongs to another install or version: run `relaybay setup --apply` again with the `relaybay` you use (the desktop program: Settings → Reconnect Claude Code). |
| `install` | The desktop program runs from a mounted disk image, macOS's translocation, the temp or the Downloads folder: install it (macOS: move relaybay.app to Applications) and start it from there. |
| `hook executable` | The installed program's `relaybay-hook` is missing or answers another version: reinstall the program. |
| `desktop start` | The program's last start ended with an exception, or before any outcome: see [relaybay did not start at login](#relaybay-did-not-start-at-login). |
| `hook spool` | Hook events have waited more than 10 minutes for the server: see [hook events waiting in the spool](#hook-events-waiting-in-the-spool). |
| `MCP registered for Claude Code` | Run the `claude mcp add …` line that `relaybay token` prints. |

## relaybay did not start at login

Start at login runs the program hidden (`--background`): after a start only the tray icon shows.
If there is no icon after you sign in:

1. Run `relaybay doctor` and read the `desktop start` item: the time of the program's last start,
   its command line, and how that start ended. The same lines are in `desktop-start.log` in the
   config folder (Windows `%LOCALAPPDATA%\relaybay`, macOS `~/Library/Application Support/relaybay`,
   Linux `~/.config/relaybay`, or `RELAYBAY_CONFIG_DIR`).

    | The last start | What it means |
    |---|---|
    | from before you signed in | The system did not start the program: check the login entry (below). |
    | `started, log <file>` | The program came up; what happened next is in that log file. |
    | `another instance answered` | A program was already running and was asked to show its window. |
    | `lock held, no answer` | Another process holds `desktop.lock` but does not answer on `desktop.ipc`: look for a `relaybay-desktop` process in the task manager, end it and start relaybay again. |
    | `exception: …` | The start failed; the traceback follows in the file. Report it with that traceback. |
    | `extra missing` | Not the installed program: install `relaybay[desktop]`. |
    | nothing after the start line | The process ended (or was ended) before any outcome. |

2. Check the login entry. `relaybay desktop config` prints `Autostart: on (<where>)` or `off`;
   Settings → Start at login turns it on. On Windows the entry is the value `relaybay` under
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, and Windows' own switch for it is
   **Settings → Apps → Startup** (relaybay must be **On** there); Task Manager → Startup apps shows the
   same. On macOS it is `~/Library/LaunchAgents/io.github.codenator81.relaybay.plist`, on Linux
   `~/.config/autostart/relaybay.desktop`.

## Hook events waiting in the spool

When the server does not answer, the command hook keeps the events that change a session's state
(start and end, a prompt, a notification, the end of a turn, a todo change) in `hook-spool.jsonl` in
the config folder (the folder of `desktop-start.log` above), at most 200 lines or 256 KB, oldest
dropped; the prompt text is not written. The next hook event that reaches the server sends them
first, oldest first, and the file empties. `relaybay doctor` shows
`hook spool: N events waiting since <time>`; it fails once the oldest has waited more than
10 minutes.

1. Start the server (the desktop program, or `relaybay serve`) and check `relaybay doctor`: the
   `relaybay server` item must be ok.
2. The spool is sent by the next hook event of any session: send a prompt or let a tool run. A
   session whose newer events already got through ignores its older spooled ones, so nothing is
   undone by a late replay.
3. Events of a session that has ended for good can be dropped: delete `hook-spool.jsonl` and any
   `hook-spool.*.part` beside it (lines of runs that found the file busy).

With an external server, update the server together with the program: a server older than the
spool takes a replayed event as a live one, so a late `SessionEnd` could end a session that started
again.

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

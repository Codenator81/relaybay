# 0016 — Desktop program: tray, window, autostart, installers

- Status: **Accepted**, planned for 0.6.0. PR1 (tray, window, autostart, external client mode) and
  PR2 (frozen builds, installers, CI; see Packaging) are built; the preview behaviour is in [SPEC §13](../SPEC.md#spec-desktop). This design is frozen in
  the last PR of 0.6.
- Size: four pull requests (below), about 6 agent-days.

## Problem

relaybay is a command (`relaybay serve`) plus a browser tab. People who do not live in a terminal
want a program: an icon that starts at login, a window with the board, one click to restart, an
installer, and updates. Nothing about the board, the MCP contract or the hooks changes: the program
runs the same server and shows the same pages.

## Principles

- The same server and the same pages: the program embeds `create_app(settings)` with the options of
  `relaybay serve`; the HTMX board is unchanged.
- The system web view, not a bundled browser: small download, OS updates.
- Per user, no admin rights, no signing needed for autostart.
- The data folder is a setting, never a move: the program never touches a database it did not
  create; pointing it at an existing folder is reversible with one setting.
- The program and the CLI read the same settings file, so `setup`, `doctor` and `token` find the
  same data the program serves.
- One program per user.

## Libraries

| Need | Choice | Licence | Notes |
|---|---|---|---|
| Window | pywebview 6.2.1 (`>=6.2.1,<7`): WebView2 + pythonnet on Windows, WKWebView + pyobjc on macOS, WebKitGTK 4.1 + PyGObject on Linux | BSD-3-Clause | pythonnet 3.2.1 (MIT) comes with it on Windows. Risk: WebView2 runtime missing (rare on Windows 10/11); Linux needs the system WebKitGTK |
| Tray | pystray 0.19.5 (`>=0.19.5,<0.20`) | LGPL-3.0 | pywebview has no tray API. It stays in the optional `desktop` extra, so the base install, the wheel's required dependencies and the Docker image do not carry it; the frozen builds keep it as separate module files (onedir) and list it in `THIRD_PARTY_LICENSES.txt` (PR2) |
| Icon images | Pillow 13.0 (`>=13.0,<14`) | MIT-CMU (HPND) | also draws the icon files at development time |
| Autostart | own code: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, `~/Library/LaunchAgents/io.github.codenator81.relaybay.plist` (RunAtLoad), `~/.config/autostart/relaybay.desktop` | stdlib | |
| Single instance | OS file lock (`msvcrt.locking` / `fcntl.flock`) + a line protocol on 127.0.0.1 with an HMAC-SHA256 challenge over a random key, socket timeouts on both sides | stdlib | `multiprocessing.connection` was dropped: its handshake has no timeout and an early close killed the listener thread |
| Freezing | PyInstaller 6.22.3, onedir | GPL-2.0-or-later with the bootloader exception | rejected: Briefcase, PyOxidizer, Nuitka; Qt (about 200 MB) |
| Installers | Inno Setup 6.7.1 per user; `.app` + `.dmg` (ad-hoc signed); `.deb` | Inno Setup licence (free, commercial use allowed) | AppImage later: its per-run mount path breaks stable hook paths |

## Settings: `desktop.toml`

In the per-user config folder (`platformdirs.user_config_dir("relaybay")`, or `RELAYBAY_CONFIG_DIR`),
not in the data folder, because one of its keys says where the data folder is:

- `data_dir`: the data folder when neither `--home` nor `RELAYBAY_HOME` is given. Precedence in
  `config.load_settings`: `--home` > `RELAYBAY_HOME` > `desktop.toml data_dir` > the per-user data dir.
- `autostart` (bool), `server_mode` (`embedded` | `external`), `server_url`.
- The external server's token lives in `external-token` beside it (0600), never in the TOML.
- PR2b adds `update_check` and `update_check_hours`; a later agent runtime is a separate key.

External mode: `Settings.base_url` and `token_path` return `server_url` and `external-token`, so
`relaybay token`, `hook`, `setup` and `doctor` talk to the remote server; `relaybay serve` and the
program's own server ignore it. `relaybay token --rotate` is refused there (the token belongs to the
server), and a missing `external-token` is an error in `token` and `setup`, never a fresh random
token; `doctor` points to `relaybay desktop config --token-file`. The `serve --reload` child reads
its settings as a server too. Plain `http://` to another machine sends the token in clear text:
the Settings answer, `desktop config` and the log warn ("use https or an SSH tunnel"). A
relative `data_dir` is refused. A server (`serve`, the embedded one) never fails over
`desktop.toml`: keys of a newer version are skipped, a broken file is a logged warning and its
settings are left out; `relaybay desktop config` with options writes a broken file anew.

`healthz` checks bypass the system proxy only for loopback hosts.

`relaybay desktop config --home/--mode/--url/--token-file/--autostart on|off` shows and changes the
file; the program's Settings window does the same.

## Runtime

Package `relaybay.desktop`: `settings` (desktop.toml), `app` (`DesktopController`, `main`),
`server_thread` (`EmbeddedServer`), `tray` (menu model and pystray), `window`, `settings_window` with
`settings.html`, `autostart`, `single_instance`.

- **Threads.** The GUI loop (pywebview) owns the main thread. Windows: the tray runs in its own
  thread (`icon.run()`). macOS and Linux: `icon.run_detached()` before `webview.start()`, so the tray
  rides the Cocoa / GTK loop. No web view (Linux without WebKitGTK): the tray takes the main thread
  and "Open board" opens the browser. The server runs `uvicorn.Server.run()` in a non-daemon thread
  on a socket the program binds itself; start-up waits for `/healthz`.
- **Signals.** uvicorn 0.54 installs signal handlers only on the main thread
  (`Server.capture_signals` returns early elsewhere; verified for the pinned version), so the server
  thread leaves the GUI's signals alone. Stop: `should_exit`, join 5 s, then `force_exit`.
- **Port taken.** The program binds the port before uvicorn (Windows: `SO_EXCLUSIVEADDRUSE`, so it
  can never share a live listener), and on failure asks `/healthz` who holds it: "Port 8765 is used
  by another relaybay (version 0.5.0). Stop it, or switch to an external server." The window shows
  the message, a native dialog offers Settings. No second server.
- **Window.** `webview.create_window("relaybay", ...)` on a local "starting" page, then
  `<base>/login?token=…&next=/`. Private mode (pywebview's default): no cookies kept, each start signs
  in again with the login link, which carries the token: a local URL in embedded mode, the remote
  server's URL in external mode. The access log redacts it. Closing the window
  hides it; Quit is in the tray. Links with `target="_blank"` open in the default browser
  (`OPEN_EXTERNAL_LINKS_IN_BROWSER`, default on: the WebView2 backend's `NewWindowRequested` calls
  `webbrowser.open`). A `claude://` link is a plain navigation that pywebview does not intercept;
  WebView2 hands an unknown scheme to the OS with its own consent prompt (unverified on this
  machine, see open questions).
- **Tray menu** (built as data from the state, then turned into pystray items): Open board (default
  item), Open in browser (the login link exactly as `relaybay token` prints it), Restart server
  (Reconnect in external mode), Logs, Settings → (Start at login [checkbox], Data folder..., Server...),
  Quit relaybay. PR2b adds "Update to X.Y.Z" and "Check for updates".
- **Restart.** Embedded: stop the server, read the settings again (desktop.toml, config.toml, the
  rules), start, reload the window. External: read the settings, reload the window.
- **Quit.** Wait for a restart in progress (up to 60 s), stop the server, stop the tray, destroy the
  windows, release the lock. A restart after Quit does nothing; a Quit during start-up ends the
  start before the GUI loop.
- **Single instance.** `desktop.lock` (OS lock, released when the process dies) and `desktop.ipc`
  (port and key, 0600). The server sends a nonce, the client answers `HMAC(key, nonce) message`,
  the server answers `ok` and runs the handler; every connection has its own thread and a 2 s
  timeout, so a peer that stalls or closes early, or a handler that fails, never stops the
  listener. A second start sends `raise` and exits 0; `relaybay desktop --quit` sends `quit` (a
  clean stop for scripts and uninstallers).
- **Autostart.** `--background` starts hidden. With `autostart = true` each start rewrites the entry
  with the current executable, so an upgrade or a move heals it. The command is the frozen or
  `relaybay-desktop` executable, else `pythonw -m relaybay.desktop` (no console at login).
- **Logs.** `logs.desktop_log_config(path)`: uvicorn's config plus one rotating file
  `<data dir>/logs/relaybay.log` (5 x 1 MB) for every logger, access lines redacted; console handlers
  only when the process has a console (a windowed start has `sys.stderr = None`).
- **Settings window.** A local page (in external mode the board is remote) with `app.css` inlined,
  talking to Python through pywebview's `js_api`: data folder with a folder dialog, server mode, URL
  and token, start at login. Saving a server change restarts the server.

### Icon

The mark (variant A of the design sheet): a rounded blue square (`#3b5bdb`) with a white diagonal
baton from a filled node to a ring: a hand-off between sessions. The source is
`packaging/icons/relaybay.svg` (64 x 64 viewBox); `packaging/icons/make_icons.py` draws it with
Pillow's primitives from the same numbers (no SVG renderer dependency) and writes `relaybay.png`
(1024), `relaybay.ico` (16–256), `relaybay.icns` (16–1024), the board favicon (`favicon.png`, 32)
and the tray icons. At 16 px the colour icon uses the thicker strokes of the design sheet (baton 10,
nodes radius 10, ring stroke 7, corner 16). The tray uses monochrome variants without the square
(dark figure for a light panel, white for a dark one, 32 px and a 16 px drawing for 100 % scale)
chosen by the panel theme the OS reports (Windows `SystemUsesLightTheme`, macOS
`AppleInterfaceStyle`), else the colour icon. Generated files are committed.

### Packaging

Built in PR2; the steps per OS are in `packaging/README.md`.

- **Frozen folder.** `packaging/relaybay.spec`, PyInstaller 6.22.3 (pinned in the `build` dependency
  group), onedir: three executables over one shared `_internal/` folder. `relaybay` (console) and
  `relaybay-desktop` (windowed, the icon) each come from their own Analysis of a two-line launcher in
  `packaging/launchers/`; `relaybay-hook` (console) is a third Analysis of `relaybay.hook_client`
  alone, with the web, MCP and GUI packages in `excludes` (a test keeps its imports to the standard
  library). The data files are `collect_data_files("relaybay")` plus `copy_metadata("relaybay")`, so
  the frozen `--version` is the package's; `LICENSE` and `THIRD_PARTY_LICENSES.txt` go in too. The
  hooks of `pyinstaller-hooks-contrib` cover uvicorn, pywebview, pythonnet and pystray; no custom
  hook is needed. `setuptools` and `pkg_resources` are excluded: PyInstaller's own hooks would
  otherwise collect setuptools' vendored packages into a program that never uses them.
- **Windows.** `packaging/windows/relaybay.iss`, Inno Setup 6.7.1 (installed by CI with Chocolatey):
  per user (`PrivilegesRequired=lowest`) into `%LOCALAPPDATA%\Programs\relaybay`, a fixed `AppId`,
  a Start Menu entry, tasks "Start relaybay at login" (off; it runs
  `relaybay desktop config --autostart on`, so `desktop.toml` and the Run value agree) and "Add
  relaybay to PATH for this user" (on). The program holds the named mutex `relaybay-desktop` while
  it runs (a failure to create it is a logged warning). Setup does not use Inno's `AppMutex`: Inno
  checks it at start-up with an OK/Cancel box, in an order against the script's own code that is not
  documented. Instead, after the wizard and before any file is written (`PrepareToInstall`), setup
  sends `relaybay desktop --quit` to a running program, waits up to 30 s for the mutex to go, and
  leaves a program that does not answer to the Restart Manager (`CloseApplications`); after the
  files are in place it starts the new `relaybay-desktop --background` again when one was running.
  The uninstaller sends the same `--quit` first and stops with a message when the program is still
  running 30 s later. An upgrade replaces `_internal` as a whole. The uninstaller removes the PATH
  entry and the Run value when it points into the install folder; the data folder and
  `desktop.toml` are outside it and stay. `desktop.toml` stays the source of truth for start at
  login: the uninstaller does not edit it, and a program started with `autostart = true` writes the
  Run value again (the same healing as after a move), so a reinstall comes back as it was.
- **macOS.** `BUNDLE` makes `relaybay.app` (`io.github.codenator81.relaybay`, `relaybay-desktop` is
  the bundle's executable, a Dock icon, ad-hoc signed by PyInstaller, a signing error fails the
  build); `packaging/macos/build_dmg.sh` makes the `.dmg` with `hdiutil create` and an
  `Applications` link. arm64 only, not notarized.
- **Linux.** `packaging/linux/build_deb.sh` on ubuntu-22.04 (glibc 2.35 floor) with `dpkg-deb`:
  `/opt/relaybay`, `/usr/bin/relaybay`, a desktop entry and the SVG icon; `Depends: libc6 (>= 2.35)`,
  `Recommends: xdg-utils`. The frozen interpreter cannot import the system's `python3-gi` (built for
  the system Python), and PyGObject with WebKitGTK is not bundled, so the Linux build has no board
  window: the tray (pystray's X11 backend) and the board in the browser. A window on Linux needs
  PyGObject in the build and a check that WebKitGTK from the system loads next to the bundled GTK
  libraries: later.
- **Names.** `relaybay-<v>-windows-x64-setup.exe`, `relaybay-<v>-macos-arm64.dmg`,
  `relaybay_<v>_amd64.deb`; a release adds `SHA256SUMS.txt` over them, the wheel and the sdist.
- **CI.** `.github/workflows/desktop.yml` (called by `ci.yml` on every pull request, artifacts kept
  7 days, and by `release.yml`) builds on the three runners and checks the frozen program: the
  version, `serve` with `/healthz` and an MCP `initialize`, `relaybay-hook` timed against a live
  local server answering `200 application/json {}` (exit 0 and `{}` on stdout, median of 20 runs at
  most 300 ms on every OS, the interpreter's time printed beside it) and once against a dead port
  (exit 0, no output, not timed: Windows retries a refused loopback connect until the hook's
  0.3 s timeout), `relaybay desktop --exit-after 10` with `/healthz` up meanwhile (under `xvfb-run`
  on Linux); then the installed one: silent setup, `--version`, user PATH, an upgrade over the
  running program (it is stopped and started again), a silent uninstall while it runs (process gone,
  files and PATH entry removed); the mounted `.dmg`; `dpkg -i`. The macOS job may fail without
  failing the run (`continue-on-error`), and a release goes out without the `.dmg` then; the
  Windows and Linux installers are required.

## Pull requests

1. **PR1** "feat: desktop program with tray, window and autostart": the package above,
   `relaybay desktop` and `desktop config`, the data-folder precedence and external mode in `config.py`,
   `desktop_log_config`, the `desktop` extra, `relaybay-hook` and `relaybay-desktop` entry points,
   the icons, tests without a GUI plus a Windows-only test that opens the real window.
2. **PR2** "build: frozen builds and installers": PyInstaller spec (relaybay console, relaybay-desktop
   windowed, relaybay-hook console; `copy_metadata`), Inno Setup script, macOS bundle and `.dmg`,
   `.deb`, `THIRD_PARTY_LICENSES.txt`, a `desktop.yml` CI workflow with smokes per OS and the hook
   start-up timing.
3. **PR2b** "feat: update check and in-app update": GitHub releases check, SHA-256 verification,
   backup of the database and config before a new version, the Inbox line and the tray item.
4. **PR3** "feat: setup and doctor for the installed program; desktop docs": install kind, the frozen
   hook command, doctor items, Reconnect in Settings, how-tos (install, Docker server with the
   desktop client, uninstall), the full SPEC section.

## Risks

macOS wants the main thread for both the tray and the window (`run_detached`; checked only by the CI
smoke until someone runs a Mac) | GNOME shows no tray without the AppIndicator extension; no
WebKitGTK means tray only | WebView2 runtime missing | the external-mode token over plain HTTP on a
LAN (the how-to requires TLS or an SSH tunnel) | browser notifications denied inside a web view (the
board falls back) | a frozen build reporting version 0.0.0 (`copy_metadata` plus a smoke).

## Open questions, defaults taken

1. macOS dock icon: a normal app with a dock icon. 2. macOS: arm64 only (Intel via `uvx`). 3. Linux:
the `.deb` only in 0.6. 4. Real Mac / GNOME test: CI smokes only, macOS and Linux marked beta. 5.
Backup scope: database, config, policy, token; not attachments; keep 5. 6. MCP registration from the
program: show the `claude mcp add` command. 7. Update check: on by default, `update_check = false`
turns it off. 8. Closing the window hides it to the tray; Quit only from the tray. 9. `claude://`
links in the window: left to the web view's own external-scheme handling in PR1; if the owner's check
shows nothing opens, PR3 adds a click handler that passes them to the OS.

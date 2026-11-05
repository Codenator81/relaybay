# Building the desktop program

The installers wrap one PyInstaller "onedir" folder with three executables that share an
`_internal/` folder:

| Executable | What it is |
|---|---|
| `relaybay` | the command line (console): `serve`, `setup`, `doctor`, `desktop`, ... |
| `relaybay-desktop` | the program with the tray icon and the board window (no console window) |
| `relaybay-hook` | the Claude Code hook forwarder, built from `relaybay/hook_client.py` with the standard library only, so it starts fast |

Pinned tools: PyInstaller 6.22.3 (the `build` dependency group in `pyproject.toml`; GPL-2.0-or-later
with the bootloader exception) and Inno Setup 6.7.1 on Windows (Inno Setup licence, free for
commercial use). What the builds bundle is listed in `THIRD_PARTY_LICENSES.txt`.

## Every OS: the frozen folder

```bash
uv sync --extra desktop --group build
uv run --no-sync pyinstaller --noconfirm packaging/relaybay.spec
dist/relaybay/relaybay --version        # macOS: dist/relaybay.app/Contents/MacOS/relaybay
uv run --no-sync python scripts/hook_timing.py -- dist/relaybay/relaybay-hook
```

The spec refuses to build when `relaybay` is not installed in the environment (`uv sync`), since the
frozen program reads its version from the package metadata.

## Windows: the setup program

Install [Inno Setup 6](https://jrsoftware.org/isinfo.php), then:

```powershell
iscc /DVersion=0.6.0 packaging\windows\relaybay.iss
```

Output: `dist\relaybay-0.6.0-windows-x64-setup.exe`. It installs per user without admin rights into
`%LOCALAPPDATA%\Programs\relaybay`, with a Start Menu entry and two choices: start relaybay at login
(off by default) and add it to the user `PATH` (on). Silent install for scripts:
`setup.exe /VERYSILENT /CURRENTUSER`.

An upgrade asks a running program to quit (`relaybay desktop --quit`), installs, and starts the new
version again in the tray, also when it runs silently. The uninstaller asks it to quit the same way
and stops with a message if it is still running after 30 seconds.

The uninstaller only knows the program. A server you started yourself from the installed
`relaybay` (`relaybay serve`, for example the server of an external-mode setup) keeps
`relaybay.exe` open: stop it before uninstalling, or its files stay behind until the next restart
of Windows.

The uninstaller keeps the data folder and `desktop.toml` (both outside the install folder). It
removes the start-at-login entry when that entry points into the install folder, but leaves
`autostart` in `desktop.toml` as it is: the file is the source of truth, and the program writes
the entry again at its next start when `autostart = true`, so a reinstall comes back as it was.
To turn start at login off for good, use the program's Settings or
`relaybay desktop config --autostart off` before uninstalling.

## macOS: the disk image

```bash
bash packaging/macos/build_dmg.sh 0.6.0
```

Output: `dist/relaybay-0.6.0-macos-arm64.dmg` with `relaybay.app` (bundle id
`io.github.codenator81.relaybay`, a normal app with a Dock icon) and an `Applications` link. The
app is ad-hoc signed and not notarized, so Gatekeeper asks once: System Settings, Privacy and
Security, "Open Anyway".

## Linux: the Debian package

On Ubuntu 22.04 (its glibc 2.35 is the oldest the package runs on):

```bash
bash packaging/linux/build_deb.sh 0.6.0
```

Output: `dist/relaybay_0.6.0_amd64.deb`: the folder in `/opt/relaybay`, the `/usr/bin/relaybay`
link, a desktop entry and the icon. The Linux build has the tray icon and opens the board in the
browser; the board window (WebKitGTK through PyGObject) is not bundled yet.

## CI

`.github/workflows/desktop.yml` runs all of the above on `windows-latest`, `macos-latest` (arm64) and
`ubuntu-22.04` for every pull request and release, then checks the frozen program (version, server
with `/healthz` and an MCP `initialize`, the hook start-up time, `relaybay desktop --exit-after 10`)
and the installed one (silent setup, an upgrade over the running program, an uninstall while it
runs, the mounted disk image, `dpkg -i`). The hook is timed against a live local server (median of
20 runs, at most 300 ms on every OS); against a dead port it must only exit 0 without output. A
failed macOS job does not fail the run, and a release then ships without the `.dmg`.

# ADR-0002: Desktop shell: pywebview and pystray, PyInstaller, per-user installers

- Status: accepted
- Date: 2026-11-05

## Context

relaybay 0.6 adds a desktop program: a tray icon, the board in a window, the server in the same
process, start at login, installers for Windows, macOS and Linux and in-app updates
([design 0016](../design/0016-desktop-program.md)). The board is server-rendered HTMX
([ADR-0001](0001-htmx-sse.md)) and must stay the same page in the browser and in the program. The
Claude Code hooks start a forwarder on every hook event, so its path must be stable and its start-up
fast. The project has one maintainer and no code-signing certificates.

## Options

1. **Window and tray:** (a) pywebview (the system web view: WebView2, WKWebView, WebKitGTK) plus
   pystray for the tray; (b) Qt through PySide6 (QtWebEngine and QSystemTrayIcon); (c) Electron or
   Tauri around the existing server.
2. **Freezing:** (a) PyInstaller onedir; (b) Briefcase; (c) PyOxidizer or Nuitka.
3. **Windows installer:** (a) Inno Setup, per user without admin rights; (b) MSIX; (c) WiX MSI.

## Decision

1a, 2a, 3a.

- **pywebview + pystray** reuse the web engine the OS already has: a small download, security
  updates come with the OS, and the HTMX board runs unchanged. Qt bundles Chromium (about 200 MB)
  and a second UI toolkit; Electron or Tauri bring a Node or Rust toolchain into a Python project.
  pywebview has no tray API, so pystray (LGPL-3.0, shipped as separate module files in the onedir
  build) draws the icon.
- **PyInstaller onedir** has maintained hooks for uvicorn, pywebview, pythonnet and pystray, builds
  three executables over one shared folder (`relaybay`, `relaybay-desktop`, `relaybay-hook`) and
  never unpacks to a temp folder at each start, which keeps the hook forwarder's start-up in tens of
  milliseconds and its path stable. Briefcase wants its own project layout and packaging model;
  PyOxidizer and Nuitka have fewer ready hooks for this stack and were not taken further.
- **Inno Setup per user** (`PrivilegesRequired=lowest`, `%LOCALAPPDATA%\Programs\relaybay`)
  installs without admin rights and needs no certificate. MSIX needs a trusted signing
  certificate; WiX is heavier for the same result.
- macOS ships an ad-hoc signed `.app` in a `.dmg` (arm64), Linux a `.deb` into `/opt/relaybay`.
  AppImage is deferred: its mount path changes on every run, which breaks the hook paths written
  into Claude Code's settings.

## Consequences

- Without certificates the OS warns on first start (SmartScreen, Gatekeeper); the install guide
  shows how to get past it, and macOS and Linux builds are marked beta.
- The Linux build has no board window yet (the frozen interpreter cannot load the system's
  PyGObject); the tray opens the board in the browser.
- The window depends on the system web view: WebView2 on Windows (present on current Windows 10 and
  11), WebKitGTK on Linux.
- `relaybay setup` from the installed program writes hooks that start the bundled `relaybay-hook`
  by its full path; it refuses while the program runs from a place that goes away (a mounted disk
  image, Gatekeeper's translocation, the temp or Downloads folder).
- Updates download the next release's installer and check it against `SHA256SUMS.txt` of the same
  release: integrity, not authenticity, since nothing is signed.

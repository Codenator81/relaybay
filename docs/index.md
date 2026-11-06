# relaybay

A live board and MCP server for people who run many Claude Code sessions across many repositories.
Sessions report their work through MCP tools and Claude Code hooks; you watch
**projects → tasks → stages → reports** update in the browser, on the desktop or on your phone.

Local and private: one process, SQLite, bound to `127.0.0.1`, token-protected, no telemetry. The
only outbound calls are a daily check of the latest release on GitHub (`update_check = false` in
`config.toml` turns off the server's, the same key in `desktop.toml` the desktop program's) and, when
you click **Update to X.Y.Z** in the program, the download of that release's installer.

## Where to start

- **Getting started** — [install and run](getting-started/install.md) (the desktop program's
  installer for Windows, macOS or Linux, or `uv tool install relaybay`), then
  [connect Claude Code](connect-claude-code.md), or let your agent do both:
  [install with your coding agent](getting-started/agent-install.md).
- **How-to guides** — [coordinate many projects](how-to/coordinator.md),
  [import and export markdown](how-to/markdown-import-export.md),
  [intake from chats and e-mail](how-to/intake.md),
  [phone and LAN access](how-to/lan-access.md),
  [screenshots for review](how-to/screenshots-for-review.md),
  [troubleshooting](how-to/troubleshooting.md),
  [full access on a trusted machine](how-to/trusted-machine.md),
  [move to the desktop program](how-to/desktop-from-source-server.md),
  [Docker server with the desktop client](how-to/docker-server-with-desktop-client.md),
  [uninstall](how-to/uninstall.md).
- **Reference** — [specification](SPEC.md), [MCP tools guide](mcp-tools.md),
  [MCP reference](reference/mcp.md), [CLI](reference/cli.md),
  [configuration](reference/configuration.md), [policy](reference/policy.md),
  [roadmap](ROADMAP.md), [changelog](changelog.md).
- **Explanation** — [architecture](architecture.md), [design documents](design/index.md).

For AI tools: [`llms.txt`](llms.txt) is a short index of these pages.

relaybay is open source under the MIT license:
[github.com/Codenator81/relaybay](https://github.com/Codenator81/relaybay).

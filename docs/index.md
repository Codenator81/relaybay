# relaybay

A live board and MCP server for people who run many Claude Code sessions across many repositories.
Sessions report their work through MCP tools and Claude Code hooks; you watch
**projects → tasks → stages → reports** update in the browser, on the desktop or on your phone.

Local and private: one process, SQLite, bound to `127.0.0.1`, token-protected, no telemetry, no
outbound calls.

## Where to start

- **Getting started** — [install and run](getting-started/install.md) (`uv tool install
  relaybay`), then [connect Claude Code](connect-claude-code.md), or let your agent do both:
  [install with your coding agent](getting-started/agent-install.md).
- **How-to guides** — [troubleshooting](how-to/troubleshooting.md),
  [import and export markdown](how-to/markdown-import-export.md),
  [phone and LAN access](how-to/lan-access.md), [remove the integration](how-to/uninstall.md).
- **Reference** — [specification](SPEC.md), [MCP tools guide](mcp-tools.md),
  [MCP reference](reference/mcp.md), [CLI](reference/cli.md),
  [configuration](reference/configuration.md).
- **Explanation** — [architecture](architecture.md), [design documents](design/index.md),
  [roadmap](ROADMAP.md).

For AI tools: [`llms.txt`](llms.txt) is a short index of these pages.

relaybay is open source under the MIT license:
[github.com/Codenator81/relaybay](https://github.com/Codenator81/relaybay).

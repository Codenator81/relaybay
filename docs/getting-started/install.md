# Install and run

!!! note
    Packaged installs (`uv tool install relaybay`, `uvx relaybay`, `pipx`, Docker) are coming in
    0.1.0. Until then relaybay runs from a checkout.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) on Windows, macOS or Linux.

```bash
git clone https://github.com/Codenator81/relaybay.git
cd relaybay
uv run relaybay serve
```

The server prints its data folder, a login link (`http://127.0.0.1:8765/login?token=…`) and the MCP
URL. Open the link once: the browser keeps an HttpOnly cookie and the token disappears from the
address bar. Opening the board without it shows a sign-in page where you can paste the token.

`relaybay token` prints the token again with the command that connects Claude Code:

```bash
uv run relaybay token
```

Next: [connect Claude Code](../connect-claude-code.md).

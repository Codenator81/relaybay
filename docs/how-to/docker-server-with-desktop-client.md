# Docker server, desktop program as its client

Run the relaybay server in Docker (on this machine, a home server or a small box on your LAN) and
use the desktop program as its client: the tray, the window and the Claude Code hooks on your
machine talk to that server instead of starting their own. The program calls this the external
server mode.

## 1. Start the server

[`examples/compose.yaml`](https://github.com/Codenator81/relaybay/blob/main/examples/compose.yaml)
runs the published image with the data in a volume and the port published on loopback only:

```bash
docker compose -f examples/compose.yaml up -d
docker compose -f examples/compose.yaml logs relaybay      # version, data folder, login link
```

Pin a version in `image:` (for example `ghcr.io/codenator81/relaybay:0.6.0`) so an update is your
decision; `docker compose pull` and `up -d` move it. The volume holds `relaybay.sqlite`, the
`token`, `config.toml` and `backups/` (a new version copies the database there before it migrates).

## 2. Copy the server's token

The token lives in the volume. Print it (`-T`: no terminal, so no stray carriage return):

```bash
docker compose -f examples/compose.yaml exec -T relaybay cat /data/token
```

On macOS and Linux you can save it to a file for step 4 (keep that file private, delete it after):
add `> relaybay-token.txt`. On Windows, copy the printed token and paste it into the program's
Settings in step 4 instead: Windows PowerShell 5.1 writes a redirected file as UTF-16, which is not
the plain text relaybay reads.

`relaybay token` inside the container prints it with the login link and the `claude mcp add`
command, but with the container's view of the URL.

## 3. Reach the server safely

The token is a password for the board and the MCP server. Plain `http://` to another machine sends
it in clear text, so the program and `relaybay desktop config` warn about it. Pick one:

- **Same machine:** use `http://127.0.0.1:8765`. Nothing leaves the machine.
- **SSH tunnel** (no certificate needed): on the client, keep a tunnel open and use the loopback
  URL as if the server were local:

    ```bash
    ssh -N -L 8765:127.0.0.1:8765 you@board-host
    ```

    The program and the hooks then use `http://127.0.0.1:8765`. Stop any local relaybay first:
    the tunnel needs the port.

- **TLS in front:** a reverse proxy with a certificate (Caddy, nginx, Traefik) on the server
  forwards `https://board.example.lan` to `127.0.0.1:8765`. relaybay accepts only the host names
  it knows: add the name to `RELAYBAY_ALLOWED_HOSTS` in the compose file (or `allowed_hosts` in
  `config.toml` in the volume), as the example does, and restart the container. The proxy must
  pass the board's live updates (`/events`, Server-Sent Events) through unbuffered; relaybay
  sends `X-Accel-Buffering: no` for nginx.

Do not publish the container port on `0.0.0.0` without one of these. More on names and phones:
[Phone and LAN access](lan-access.md).

## 4. Point the program at the server

In the program: tray → Settings → Server... → **Connect to an external server**, enter the URL
and paste the token, Save (the way to go on Windows). Or with the installed command line and the
token file from step 2:

```bash
relaybay desktop config --mode external --url https://board.example.lan --token-file relaybay-token.txt
```

The URL and mode go into `desktop.toml`; the token is copied into `external-token` next to it
(private to your user), never into the TOML. After that the window opens the remote board, and
`relaybay token`, `setup`, `doctor` and `hook` on this machine use that server. The program starts
no server of its own; Restart in the tray becomes Reconnect. `relaybay token --rotate` is refused
here: rotate the token on the server, then save the new one with `--token-file` again.

## 5. Connect Claude Code on the client

The hooks run on the machine where Claude Code runs, so set them up on the client:

```bash
relaybay setup                           # the hooks point at the server URL and external-token
relaybay setup --apply --expect <hash>
relaybay token                           # prints the claude mcp add command with the server URL
```

Run the `claude mcp add` line it prints; setup never writes `~/.claude.json`. The program's
Settings → **Reconnect Claude Code** shows the same plan. Then check everything:

```bash
relaybay doctor          # "server mode: external https://board.example.lan (desktop.toml)"
```

## Back to the embedded server

Settings → Server... → **Run the server in this program** (or
`relaybay desktop config --mode embedded`), then `relaybay setup --apply` again so the hooks point
at the local server and its token.

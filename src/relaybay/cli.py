"""Command line: `relaybay serve`, `relaybay token`."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from relaybay import __version__
from relaybay.config import Settings, load_settings
from relaybay.security import load_or_create_token, rotate_token, token_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relaybay", description="Live board and MCP server for Claude Code sessions."
    )
    parser.add_argument("--version", action="version", version=f"relaybay {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the server (default command).")
    serve.add_argument("--host", help="Bind address (default 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default 8765).")
    serve.add_argument("--home", type=Path, help="Data directory (default: per-user data dir).")
    serve.add_argument("--reload", action="store_true", help="Restart on code changes (dev).")

    token = sub.add_parser("token", help="Print the access token and how to connect Claude Code.")
    token.add_argument("--home", type=Path, help="Data directory.")
    token.add_argument("--rotate", action="store_true", help="Generate a new token.")
    return parser


def _mcp_add_command(settings: Settings, token: str) -> str:
    return (
        f"claude mcp add --transport http --scope user relaybay {settings.base_url}/mcp"
        f' --header "Authorization: Bearer {token}"'
    )


def running_token_id(settings: Settings) -> str | None:
    """`token_id` reported by a server already listening on the configured port, if any."""
    opener = build_opener(ProxyHandler({}))  # localhost: never go through a system proxy
    try:
        with opener.open(f"{settings.base_url}/healthz", timeout=1) as response:
            data = json.load(response)
    except (URLError, OSError, ValueError):
        return None
    value = data.get("token_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def cmd_token(settings: Settings, rotate: bool) -> int:
    path = settings.token_path
    token = rotate_token(path) if rotate else load_or_create_token(path)
    print(token)
    print()
    print(f"Data folder: {settings.home}")
    running = running_token_id(settings)
    if running and running != token_id(token):
        print(
            f"WARNING: the server on {settings.base_url} uses a different token, so this one"
            " will not work there. Either it runs with another data folder (use 'relaybay token"
            " --home <folder>' with the folder 'relaybay serve' printed) or the token was rotated"
            " and the server needs a restart.",
            file=sys.stderr,
        )
    print()
    print("Connect Claude Code (user scope, all repositories):")
    print(f"  {_mcp_add_command(settings, token)}")
    print()
    print(f"Open the board: {settings.base_url}/login?token={token}")
    if rotate:
        print()
        print("The token was rotated: restart 'relaybay serve' and update the Claude Code entry.")
    return 0


def cmd_serve(settings: Settings, reload: bool) -> int:
    import uvicorn

    from relaybay.app import create_app
    from relaybay.logs import uvicorn_log_config

    token = load_or_create_token(settings.token_path)
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: listening on {settings.host}: the board is reachable from other machines."
            " Add the names you use to allowed_hosts in config.toml.",
            file=sys.stderr,
        )
    print(f"relaybay {__version__}", flush=True)
    print(f"Data folder: {settings.home}", flush=True)
    print(f"Board:  {settings.base_url}/login?token={token}", flush=True)
    print(f"MCP:    {settings.base_url}/mcp  (run 'relaybay token' for the setup command)")
    print(flush=True)

    common = {
        "host": settings.host,
        "port": settings.port,
        "proxy_headers": False,
        "server_header": False,
        # SSE and MCP streams never finish on their own; do not wait for them on Ctrl+C.
        "timeout_graceful_shutdown": 2,
        "log_config": uvicorn_log_config(),
    }
    if reload:
        # The reloader imports the app in a child process; pass resolved settings via env.
        os.environ["RELAYBAY_HOME"] = str(settings.home)
        os.environ["RELAYBAY_HOST"] = settings.host
        os.environ["RELAYBAY_PORT"] = str(settings.port)
        src = Path(__file__).resolve().parent
        uvicorn.run(
            "relaybay.app:create_app_from_env",
            factory=True,
            reload=True,
            reload_dirs=[str(src)],
            **common,
        )
    else:
        uvicorn.run(create_app(settings), **common)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    try:
        settings = load_settings(
            home=getattr(args, "home", None),
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
    except (ValueError, OSError) as exc:
        print(f"relaybay: {exc}", file=sys.stderr)
        return 2
    if command == "token":
        return cmd_token(settings, args.rotate)
    return cmd_serve(settings, getattr(args, "reload", False))

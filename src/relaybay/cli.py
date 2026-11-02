"""Command line: `relaybay serve`, `token`, `setup`, `doctor`, `demo` and `hook`."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from relaybay import __version__
from relaybay.config import Settings, load_settings
from relaybay.domain.hooks import HOOK_PATH
from relaybay.machine_setup import (
    DEFAULT_MODE,
    MODES,
    Check,
    SetupError,
    claude_paths,
    detect,
    format_plan,
    make_plan,
    run_check,
    write_change,
)
from relaybay.security import load_or_create_token, rotate_token, token_id


def build_parser() -> argparse.ArgumentParser:
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
    serve.add_argument(
        "--claude-home",
        type=Path,
        help="Folder holding Claude Code's .claude/ and .claude.json for the Integrations page"
        " (default: your home directory).",
    )

    token = sub.add_parser("token", help="Print the access token and how to connect Claude Code.")
    token.add_argument("--home", type=Path, help="Data directory.")
    token.add_argument("--rotate", action="store_true", help="Generate a new token.")

    setup = sub.add_parser(
        "setup", help="Connect Claude Code on this machine: hooks in user settings, MCP check."
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan, the diff and its hash; write nothing (default; with --remove:"
        " preview the removal).",
    )
    action = setup.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Write the plan (with a backup).")
    action.add_argument(
        "--remove", action="store_true", help="Remove relaybay's hooks (with a backup)."
    )
    action.add_argument("--check", action="store_true", help="Check the setup end to end.")
    setup.add_argument(
        "--expect",
        metavar="HASH",
        help="With --apply/--remove: write only if the plan still has this hash (printed by the"
        " dry run). Without it, the check covers only changes made during this run.",
    )
    setup.add_argument(
        "--json", action="store_true", help="With --check: print the results as JSON."
    )
    setup.add_argument("--home", type=Path, help="relaybay data directory.")
    setup.add_argument("--port", type=int, help="relaybay port (default 8765).")
    setup.add_argument(
        "--claude-home",
        type=Path,
        help="Folder holding .claude/settings.json and .claude.json (default: your home).",
    )
    setup.add_argument(
        "--mode",
        choices=MODES,
        default=DEFAULT_MODE,
        help="Hook type: command (default; a small forwarder, silent when relaybay is down) or"
        " http (Claude Code posts itself; needs RELAYBAY_TOKEN in its environment).",
    )
    wake = setup.add_mutually_exclusive_group()
    wake.add_argument(
        "--wake",
        action="store_true",
        help="Also add the opt-in wake-up hook (command mode, experimental): an asyncRewake hook"
        " that wakes an idle session when an owner comment arrives.",
    )
    wake.add_argument("--no-wake", action="store_true", help="Remove the wake-up hook.")
    setup.add_argument(
        "--force-wake",
        action="store_true",
        help="With --wake: install it although no Claude Code version is known to run asyncRewake"
        " hooks in the background (one that does not would hold every turn's end).",
    )

    doctor = sub.add_parser(
        "doctor", help="Diagnose the installation: versions, data folder, server, Claude Code."
    )
    doctor.add_argument("--json", action="store_true", help="Print the results as JSON.")
    doctor.add_argument("--home", type=Path, help="relaybay data directory.")
    doctor.add_argument("--port", type=int, help="relaybay port (default 8765).")
    doctor.add_argument(
        "--claude-home",
        type=Path,
        help="Folder holding .claude/settings.json and .claude.json (default: your home).",
    )

    demo = sub.add_parser(
        "demo", help="Seed a demo workspace (acme-shop, demo-api) into a data directory."
    )
    demo.add_argument(
        "--home", type=Path, required=True, help="Data directory for the demo (required)."
    )
    demo.add_argument(
        "--force",
        action="store_true",
        help="If the folder already holds a board, rename its database aside and start over.",
    )

    hook = sub.add_parser("hook", help="Forward one hook event from stdin (command hook).")
    hook.add_argument("--home", type=Path, help="Data directory.")
    hook.add_argument("--port", type=int, help="Port (default 8765).")
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


def cmd_setup(settings: Settings, args: argparse.Namespace) -> int:
    token = load_or_create_token(settings.token_path)
    paths = claude_paths(settings.claude_home)
    if args.check:
        checks = run_check(base_url=settings.base_url, token=token, paths=paths)
        return print_checks(checks, args.json)
    if args.dry_run and args.apply:
        print("relaybay setup: --dry-run and --apply exclude each other.", file=sys.stderr)
        return 2
    plan = make_plan(
        base_url=settings.base_url,
        token=token,
        token_path=settings.token_path,
        paths=paths,
        detection=detect(),
        mode=args.mode,
        remove=args.remove,
        wake=True if args.wake else (False if args.no_wake else None),
        force_wake=args.force_wake,
    )
    write = (args.apply or args.remove) and not args.dry_run
    title = "remove" if args.remove else "apply"
    print(f"relaybay setup: {title}" + ("" if write else " (dry run)"))
    for line in format_plan(plan, remove=args.remove):
        print(line)
    if plan.error:
        return 1
    if not write:
        print()
        print("Nothing written.")
        return 0
    assert plan.hooks is not None
    try:
        backup = write_change(plan.hooks, expect=args.expect)
    except (SetupError, OSError) as exc:
        print(f"relaybay setup: {exc}", file=sys.stderr)
        return 1
    print()
    if not plan.hooks.changed:
        print("Nothing to write.")
    else:
        print(f"Wrote {plan.hooks.path}" + (f" (backup: {backup})" if backup else ""))
    return 0


def print_checks(checks: list[Check], as_json: bool) -> int:
    """`setup --check` and `doctor` output: a JSON list of {ok, name, detail} or one line per
    item; exit code 1 when an item failed."""
    if as_json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            print(f"[{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    return 0 if all(c.ok for c in checks) else 1


def cmd_doctor(settings: Settings, as_json: bool) -> int:
    from relaybay.doctor import doctor_checks

    return print_checks(doctor_checks(settings), as_json)


def cmd_demo(settings: Settings, force: bool) -> int:
    from relaybay.demo import DemoError, create_demo

    try:
        result = create_demo(settings.db_path, force=force)
    except DemoError as exc:
        print(f"relaybay demo: {exc}", file=sys.stderr)
        return 1
    if result.backup:
        print(f"Moved the previous database to {result.backup}")
    print(f"Demo workspace: {result.projects} projects, {result.tasks} tasks in {result.db_path}")
    print(f"Show it: relaybay serve --home {settings.home}")
    return 0


def cmd_hook(settings: Settings) -> int:
    from relaybay import hook_client

    url = settings.base_url + HOOK_PATH
    return hook_client.main(["--url", url, "--token-file", str(settings.token_path)])


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
    args = build_parser().parse_args(argv)
    command = args.command or "serve"
    try:
        settings = load_settings(
            home=getattr(args, "home", None),
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
            claude_home=getattr(args, "claude_home", None),
        )
    except (ValueError, OSError) as exc:
        if command == "hook":
            return 0  # a hook never fails the session
        if command == "doctor":
            return print_checks([Check(False, "settings", str(exc))], args.json)
        print(f"relaybay: {exc}", file=sys.stderr)
        return 2
    if command == "hook":
        return cmd_hook(settings)
    if command == "setup":
        return cmd_setup(settings, args)
    if command == "doctor":
        return cmd_doctor(settings, args.json)
    if command == "demo":
        return cmd_demo(settings, args.force)
    if command == "token":
        return cmd_token(settings, args.rotate)
    return cmd_serve(settings, getattr(args, "reload", False))

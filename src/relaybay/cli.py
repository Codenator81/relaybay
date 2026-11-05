"""Command line: `relaybay serve`, `token`, `setup`, `doctor`, `demo`, `import`, `export`,
`desktop` and `hook`."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from relaybay import __version__
from relaybay.config import Settings, load_settings
from relaybay.domain.errors import DomainError
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
        help="Also add the opt-in wake-up hook (command mode): an asyncRewake hook that wakes an"
        " idle session when an owner comment arrives (Claude Code 2.1.309 or later).",
    )
    wake.add_argument("--no-wake", action="store_true", help="Remove the wake-up hook.")
    setup.add_argument(
        "--force-wake",
        action="store_true",
        help="With --wake: install it although this Claude Code is older than 2.1.309 or its"
        " version is unknown (one that ignores asyncRewake would hold every turn's end).",
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

    importer = sub.add_parser(
        "import",
        help="Bring a markdown setup (project briefs, task registry, task notes, memory notes)"
        " into relaybay. Prints the plan; writes nothing without --apply.",
    )
    importer.add_argument("root", type=Path, help="The folder holding the layout.")
    importer.add_argument(
        "--apply", action="store_true", help="Write the plan (default: a dry run, nothing written)."
    )
    importer.add_argument(
        "--expect",
        metavar="HASH",
        help="With --apply: write only if the plan still has this hash (printed by the dry run)."
        " Without it, you write whatever the plan is at that moment.",
    )
    importer.add_argument(
        "--overwrite",
        action="store_true",
        help="Take the file's text over a target changed on the board since its import (as a new"
        " version; old versions stay).",
    )
    importer.add_argument(
        "--aliases",
        type=Path,
        metavar="FILE",
        help="JSON file with more column, stage and heading names, in any language.",
    )
    importer.add_argument(
        "--retro-project",
        metavar="SLUG",
        help="The project whose document 'retro' receives retro.md (skipped without it).",
    )
    importer.add_argument("--home", type=Path, help="relaybay data directory.")

    exporter = sub.add_parser(
        "export",
        help="Write documents, decisions, notes and the task registry back to the markdown layout."
        " Prints what would change; writes nothing without --apply.",
    )
    exporter.add_argument("root", type=Path, help="The folder to write to.")
    exporter.add_argument(
        "--apply", action="store_true", help="Write the files (default: a dry run)."
    )
    exporter.add_argument(
        "--force",
        action="store_true",
        help="Also overwrite or remove files changed by hand since the last export.",
    )
    exporter.add_argument(
        "--aliases",
        type=Path,
        metavar="FILE",
        help="The aliases of the import: a hand-written registry that says the same stays as it"
        " is.",
    )
    exporter.add_argument("--home", type=Path, help="relaybay data directory.")

    desktop = sub.add_parser(
        "desktop",
        help="Start the desktop program: tray icon, board window and the server (needs"
        " relaybay[desktop]); 'desktop config' shows or changes its settings.",
    )
    from relaybay.desktop import add_run_arguments
    from relaybay.desktop.settings import SERVER_MODES

    add_run_arguments(desktop)
    desktop_sub = desktop.add_subparsers(dest="desktop_command", metavar="config")
    desktop_config = desktop_sub.add_parser(
        "config",
        help="Show desktop.toml (data folder, server, autostart) or change it with the options.",
    )
    desktop_config.add_argument(
        "--home",
        type=Path,
        help="Data folder for the program and the CLI when neither --home nor RELAYBAY_HOME is"
        " given; '-' clears it. Moves nothing.",
    )
    desktop_config.add_argument("--mode", choices=SERVER_MODES, help="Server mode.")
    desktop_config.add_argument("--url", help="Base URL of the external server ('-' clears it).")
    desktop_config.add_argument(
        "--token-file",
        type=Path,
        help="File holding the external server's token; copied to external-token (0600) beside"
        " desktop.toml.",
    )
    desktop_config.add_argument(
        "--autostart", choices=("on", "off"), help="Start the program at login."
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
    if settings.external_url:
        # desktop.toml points at an external server: its token is a copy, never generated here.
        if rotate:
            print(
                f"relaybay token: the token belongs to the external server {settings.base_url};"
                " rotate it there and save the new one with 'relaybay desktop config"
                " --token-file'.",
                file=sys.stderr,
            )
            return 2
        if not path.is_file():
            print(
                f"relaybay token: {path} is missing: save the external server's token with"
                " 'relaybay desktop config --token-file <file>'.",
                file=sys.stderr,
            )
            return 2
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
    if settings.external_url and not settings.token_path.is_file():
        print(
            f"relaybay setup: {settings.token_path} is missing: save the external server's token"
            " with 'relaybay desktop config --token-file <file>'.",
            file=sys.stderr,
        )
        return 2
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


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    from relaybay import markdown_import as importer

    if args.expect and not args.apply:
        print("relaybay import: --expect goes with --apply.", file=sys.stderr)
        return 2
    try:
        root = importer.resolve_root(args.root)
        aliases = importer.read_aliases(args.aliases)
    except importer.ImportProblem as exc:
        print(f"relaybay import: {exc}", file=sys.stderr)
        return 2
    try:
        code, lines = asyncio.run(
            importer.run_import(
                settings,
                root,
                aliases=aliases,
                retro_project=args.retro_project,
                overwrite=args.overwrite,
                apply=args.apply,
                expect=args.expect,
            )
        )
    except (DomainError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"relaybay import: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return code


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    from relaybay import markdown_export as exporter
    from relaybay import markdown_import as importer

    try:
        aliases = importer.read_aliases(args.aliases)
    except importer.ImportProblem as exc:
        print(f"relaybay export: {exc}", file=sys.stderr)
        return 2
    root = args.root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        print(f"relaybay export: {args.root} is not a folder", file=sys.stderr)
        return 2
    try:
        code, lines = asyncio.run(
            exporter.run_export(settings, root, aliases=aliases, apply=args.apply, force=args.force)
        )
    except (exporter.ExportProblem, DomainError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"relaybay export: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return code


def cmd_hook(settings: Settings) -> int:
    from relaybay import hook_client

    url = settings.base_url + HOOK_PATH
    return hook_client.main(["--url", url, "--token-file", str(settings.token_path)])


def cmd_desktop_config(args: argparse.Namespace) -> int:
    from relaybay.config import resolve_home
    from relaybay.desktop import settings as desktop
    from relaybay.desktop.autostart import Autostart, launch_command

    folder = desktop.config_dir()
    changed = any(
        value is not None
        for value in (args.home, args.url, args.mode, args.token_file, args.autostart)
    )
    try:
        current = desktop.load_desktop_settings(folder)
    except (ValueError, OSError) as exc:
        if not changed:
            print(
                f"relaybay desktop config: {exc}. Fix the file, or write it anew with options.",
                file=sys.stderr,
            )
            return 2
        print(
            f"relaybay desktop config: {exc}; the file is written anew from these options.",
            file=sys.stderr,
        )
        current = desktop.DesktopSettings()
    try:
        if args.home is not None:
            current.data_dir = None if str(args.home) == "-" else args.home.expanduser().resolve()
        if args.url is not None:
            current.server_url = "" if args.url == "-" else desktop.check_url(args.url)
        if args.mode is not None:
            current.server_mode = args.mode
        token = None
        if args.token_file is not None:
            token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError(f"{args.token_file} is empty")
        if current.server_mode == "external":
            if not current.server_url:
                raise ValueError("--mode external needs --url")
            if token is None and not desktop.external_token_path(folder).is_file():
                raise ValueError("--mode external needs --token-file (the server's token)")
    except (ValueError, OSError) as exc:
        print(f"relaybay desktop config: {exc}", file=sys.stderr)
        return 2
    if args.autostart is not None:
        current.autostart = args.autostart == "on"
    autostart = Autostart()
    try:
        if token is not None:
            desktop.write_external_token(folder, token)
        if changed:
            desktop.save_desktop_settings(folder, current)
        if args.autostart == "on":
            autostart.enable(launch_command())
        elif args.autostart == "off":
            autostart.disable()
    except OSError as exc:
        print(f"relaybay desktop config: {exc}", file=sys.stderr)
        return 1
    home, source = resolve_home(None, os.environ, current)
    token_path = desktop.external_token_path(folder)
    print(f"Settings file: {desktop.settings_path(folder)}" + ("" if changed else " (unchanged)"))
    print(f"Data folder:   {home} ({source})")
    print(f"Server:        {current.server_mode}")
    if current.server_mode == "external" or current.server_url:
        print(f"Server URL:    {current.server_url or '-'}")
        print(f"Server token:  {token_path if token_path.is_file() else 'missing'}")
    entry = autostart.command()
    print(f"Autostart:     {'on (' + autostart.location() + ')' if entry else 'off'}")
    warning = desktop.cleartext_warning(current)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def cmd_serve(settings: Settings, reload: bool) -> int:
    import uvicorn

    from relaybay.app import create_app, server_options
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

    common = {**server_options(settings), "log_config": uvicorn_log_config()}
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
    if command == "desktop":
        if args.desktop_command == "config":
            return cmd_desktop_config(args)
        from relaybay.desktop.app import run

        return run(background=args.background, exit_after=args.exit_after, quit=args.quit)
    try:
        settings = load_settings(
            home=getattr(args, "home", None),
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
            claude_home=getattr(args, "claude_home", None),
            serving=command == "serve",
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
    if command == "import":
        return cmd_import(settings, args)
    if command == "export":
        return cmd_export(settings, args)
    if command == "token":
        return cmd_token(settings, args.rotate)
    return cmd_serve(settings, getattr(args, "reload", False))

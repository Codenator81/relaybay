"""`relaybay doctor`: everything an agent needs to know when the setup misbehaves, read-only.

Shares the server and Claude Code checks with `relaybay setup --check`; unlike it, it never
creates the token, and it reports the Claude Code files even when the server is down.
"""

import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import URLError
from urllib.request import OpenerDirector, ProxyHandler, build_opener

from relaybay import __version__
from relaybay.config import Settings
from relaybay.domain.rules import Rules
from relaybay.machine_setup import (
    MIN_CLAUDE_CODE_VERSION,
    Check,
    Detection,
    claude_paths,
    config_checks,
    detect,
    server_checks,
)

MIN_PYTHON = (3, 12)


def _version_text(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


def _claude_check(found: Detection) -> Check:
    name = "Claude Code"
    minimum = _version_text(MIN_CLAUDE_CODE_VERSION)
    if not found.claude_path:
        return Check(False, name, "claude not found on PATH; install Claude Code first")
    version = found.version_tuple
    if version is None:
        reason = found.claude_version or found.version_error or "no output"
        return Check(False, name, f"{found.claude_path}: version unknown ({reason})")
    detail = f"{_version_text(version)} at {found.claude_path} (minimum supported {minimum})"
    return Check(version >= MIN_CLAUDE_CODE_VERSION, name, detail)


def _data_dir_check(settings: Settings) -> Check:
    home = settings.home
    if not home.is_dir():
        detail = f"{home} does not exist yet; 'relaybay serve' creates it"
        return Check(False, "data folder", detail)
    if not os.access(home, os.W_OK):
        return Check(False, "data folder", f"{home} is not writable")
    config = "config.toml loaded" if settings.config_path.is_file() else "no config.toml"
    return Check(True, "data folder", f"{home} (writable, {config})")


def _token_check(settings: Settings) -> tuple[Check, str | None]:
    path = settings.token_path
    if settings.external_url:
        # desktop.toml points at an external server: its token is a saved copy.
        fix = "save the server's token with 'relaybay desktop config --token-file <file>'"
        missing, empty = fix, fix
    else:
        missing, empty = "'relaybay serve' creates it", "run 'relaybay token --rotate'"
    if not path.is_file():
        return Check(False, "token file", f"{path} missing; {missing}"), None
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        return Check(False, "token file", f"{path} is empty; {empty}"), None
    if os.name == "posix" and path.stat().st_mode & 0o077:
        return Check(False, "token file", f"{path} is readable by others; chmod 600 it"), token
    return Check(True, "token file", f"{path} (private)"), token


def _healthz(opener: OpenerDirector, base_url: str) -> dict[str, Any] | None:
    try:
        with opener.open(f"{base_url}/healthz", timeout=5) as response:
            data = json.load(response)
    except (URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rules_check(settings: Settings, health: dict[str, Any] | None) -> Check:
    local = Rules(settings.rules, mode=settings.rules_mode).hash
    detail = f"{local} ({settings.rules_mode} mode, from the settings)"
    if health is None:
        return Check(True, "rules hash", detail + "; server not reachable to compare")
    running = health.get("rules_hash")
    if running == local:
        return Check(True, "rules hash", detail + ", the server uses the same")
    if running is None:
        version = health.get("version", "unknown")
        return Check(True, "rules hash", detail + f"; server {version} does not report one")
    return Check(
        False,
        "rules hash",
        f"{local} in the settings, but the server runs with {running}: restart 'relaybay serve'"
        " after editing config.toml",
    )


def doctor_checks(
    settings: Settings,
    *,
    detection: Detection | None = None,
    opener: OpenerDirector | None = None,
    environ: Mapping[str, str] = os.environ,
    run: Callable[..., Any] = subprocess.run,
) -> list[Check]:
    """All items, each green or red. Reads files and talks to the server; writes nothing."""
    opener = opener or build_opener(ProxyHandler({}))  # localhost: never through a proxy
    found = detection if detection is not None else detect()
    python = sys.version_info[:3]
    checks = [
        Check(True, "relaybay", f"{__version__} ({sys.executable})"),
        Check(
            python >= MIN_PYTHON,
            "Python",
            f"{_version_text(python)} (minimum {_version_text(MIN_PYTHON)})",
        ),
        Check(True, "OS", f"{platform.system()} {platform.release()} ({sys.platform})"),
        _data_dir_check(settings),
    ]
    token_item, token = _token_check(settings)
    checks.append(token_item)
    checks.append(_claude_check(found))
    checks.append(Check(True, "desktop app", found.desktop_app or "not found (optional)"))

    if token is None:
        checks.append(
            Check(False, "relaybay server", f"{settings.base_url}: not checked without a token")
        )
        health = None
    else:
        checks.extend(server_checks(base_url=settings.base_url, token=token, opener=opener))
        health = _healthz(opener, settings.base_url)
    checks.append(_rules_check(settings, health))
    checks.extend(
        config_checks(
            base_url=settings.base_url,
            token=token,
            paths=claude_paths(settings.claude_home),
            environ=environ,
            run=run,
        )
    )
    return checks

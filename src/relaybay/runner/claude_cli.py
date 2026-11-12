"""Finding the user's `claude` and building one turn's command line and environment."""

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from relaybay.domain.errors import ValidationError
from relaybay.domain.hooks import RUN_ID_ENV

BATCH_SUFFIXES = (".cmd", ".bat")
ADD_DIRS_CLAUDE_MD_ENV = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"
# cmd.exe parses a batch file's command line again ("BatBadBut"): an argument with one of these
# could run something else, so such a turn is refused. The prompt goes on stdin there.
UNSAFE_BATCH_CHARS = frozenset('"%^&|<>!\r\n')
# Set by a Claude Code session (or the desktop app) around the server; a run's `claude` is its
# own top-level session and must not take them over (relaybay.domain.hooks, tests/e2e/conftest).
HOST_VARIABLES = (
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)


def _install_folders(platform: str, environ: Mapping[str, str], home: Path) -> list[Path]:
    """The usual places of the CLI when PATH misses it: a GUI program on macOS gets launchd's
    short PATH, and on Windows PATH may have changed since login (unverified as complete)."""
    if platform == "win32":
        profile = Path(environ.get("USERPROFILE") or home)
        folders = [profile / ".local" / "bin" / "claude.exe"]
        if environ.get("APPDATA"):
            folders.append(Path(environ["APPDATA"]) / "npm" / "claude.cmd")
        return folders
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]


def find_claude(
    configured: Path | None,
    environ: Mapping[str, str] = os.environ,
    *,
    which: Callable[..., str | None] = shutil.which,
    platform: str = sys.platform,
    home: Path | None = None,
) -> str | None:
    """`claude_path` from the settings, else `claude` on PATH, else the usual install folders."""
    if configured is not None:
        return str(configured) if configured.is_file() else None
    found = which("claude", path=environ.get("PATH"))
    if found:
        return found
    for candidate in _install_folders(platform, environ, home or Path.home()):
        if candidate.is_file():
            return str(candidate)
    return None


def is_batch(path: str) -> bool:
    return path.lower().endswith(BATCH_SUFFIXES)


@dataclass(frozen=True, slots=True)
class TurnSpec:
    claude: str
    prompt: str
    cwd: str
    model: str
    effort: str
    mode: str
    allowed: tuple[str, ...] = field(default_factory=tuple)
    resume: str | None = None
    chrome: bool = False
    add_dirs: tuple[str, ...] = field(default_factory=tuple)
    settings_file: str | None = None  # relaybay's hooks, when the user's settings lack them
    mcp_file: str | None = None  # relaybay's MCP server, likewise


def build_command(spec: TurnSpec) -> tuple[list[str], str | None]:
    """argv and the text for stdin (None: nothing). The prompt follows `-p` in argv, or goes on
    stdin for a batch shim; each allowed-tools rule, extra folder and config file gets its own
    flag with one value, and no positional comes after a flag. Never --bare, --setting-sources,
    --strict-mcp-config or a skip of permissions: the turn loads what a manual session loads."""
    batch = is_batch(spec.claude)
    argv = [spec.claude, "-p"]
    if not batch:
        # A text that starts with "-" would be read as an option; a leading space keeps it text.
        argv.append(" " + spec.prompt if spec.prompt.startswith("-") else spec.prompt)
    argv += [
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--permission-mode",
        spec.mode,
    ]
    for rule in spec.allowed:
        argv += ["--allowedTools", rule]
    for folder in spec.add_dirs:
        argv += ["--add-dir", folder]
    if spec.settings_file:
        argv += ["--settings", spec.settings_file]
    if spec.mcp_file:
        argv += ["--mcp-config", spec.mcp_file]
    if spec.chrome:
        argv.append("--chrome")
    if spec.resume:
        argv += ["--resume", spec.resume]
    if batch:
        for word in argv:
            bad = sorted(set(word) & UNSAFE_BATCH_CHARS)
            if bad:
                shown = " ".join(repr(c) for c in bad)
                raise ValidationError(
                    f"{word[:80]!r} holds {shown}, which a {Path(spec.claude).name} shim would"
                    " let cmd.exe read as a command; set claude_path to claude.exe or change it."
                )
        return argv, spec.prompt
    return argv, None


def child_env(base: Mapping[str, str], run_id: int) -> dict[str, str]:
    """The server's environment, without the host session's variables, with the run's id and
    the switch that loads the `CLAUDE.md` of each --add-dir folder (the cwd alone gives only its
    own). The Claude Code config folder and any key variable stay as the user has them."""
    env = {k: v for k, v in base.items() if k not in HOST_VARIABLES}
    env[RUN_ID_ENV] = str(run_id)
    env[ADD_DIRS_CLAUDE_MD_ENV] = "1"
    return env

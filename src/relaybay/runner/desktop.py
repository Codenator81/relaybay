"""Opening a run's Claude Code session in the Claude desktop app: `claude --desktop --resume <id>`.

The CLI refuses `--desktop` when its stdout or stderr is a pipe (exit 1), but not when stderr goes
to a file: on Windows the command runs through `cmd.exe` in a console of its own, started
minimized and not focused, with stderr redirected by cmd into a file, so a refusal can be shown.
macOS and Linux would need a terminal (a PTY) for it, which relaybay does not make: there the owner
runs the terminal command instead. `--desktop` takes no `--add-dir`: in the desktop app only the
cwd is a working folder.
"""

import contextlib
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from relaybay.domain.errors import ValidationError
from relaybay.domain.hooks import RUN_ID_ENV
from relaybay.runner.claude_cli import HOST_VARIABLES, UNSAFE_BATCH_CHARS

DESKTOP_TIMEOUT_SECONDS = 60.0
REASON_MAX = 500
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
# SW_SHOWMINNOACTIVE: a minimized console that does not take the focus. A hidden one would very
# likely count as interactive too (it is still a console), but that is not verified.
_SHOW_MINIMIZED_NO_FOCUS = 7
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# A cmd.exe command line, cwd, environment, timeout -> exit code; raises OSError or
# subprocess.TimeoutExpired.
Launcher = Callable[[str, str, dict[str, str], float], int]


def desktop_command_line(claude: str, claude_session_id: str, stderr_file: Path) -> str:
    """`cmd.exe /d /c ""<claude>" --desktop --resume <id> 2> "<file>""`, passed to CreateProcess
    as written. cmd parses it once more, so a path with a character it would act on is refused."""
    for name, text in (("claude", claude), ("the data folder", str(stderr_file))):
        bad = sorted(set(text) & UNSAFE_BATCH_CHARS)
        if bad:
            shown = " ".join(repr(c) for c in bad)
            raise ValidationError(
                f"The path of {name} holds {shown}, which cmd.exe would read as a command; the"
                " desktop app cannot be opened from relaybay with it."
            )
    inner = f'"{claude}" --desktop --resume {claude_session_id} 2> "{stderr_file}"'
    return f'cmd.exe /d /c "{inner}"'


def terminal_command(claude_session_id: str) -> str:
    return f"claude --resume {claude_session_id}"


def desktop_env(base: Mapping[str, str]) -> dict[str, str]:
    """The server's environment without the host session's variables and without the run's id:
    the desktop session is the owner's, its hooks report as an ordinary session."""
    return {k: v for k, v in base.items() if k not in HOST_VARIABLES and k != RUN_ID_ENV}


def take_reason(stderr_file: Path) -> str:
    """What the command wrote to stderr, without colour codes, on one line and shortened; the
    file is removed."""
    try:
        text = stderr_file.read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    finally:
        with contextlib.suppress(FileNotFoundError):
            stderr_file.unlink()
    text = " ".join(_ANSI_RE.sub("", text).split())
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1] + "…"


def _kill_tree(pid: int) -> None:
    """cmd.exe, a claude.cmd shim and the CLI behind them: the whole tree goes."""
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(pid)],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def launch_in_console(command_line: str, cwd: str, env: dict[str, str], timeout: float) -> int:
    """Windows: the command line in a new console with no redirected handle; its exit code. Past
    the timeout the process tree is killed and TimeoutExpired raised."""
    info = subprocess.STARTUPINFO()
    info.dwFlags |= STARTF_USESHOWWINDOW
    info.wShowWindow = _SHOW_MINIMIZED_NO_FOCUS
    proc = subprocess.Popen(
        command_line,
        cwd=cwd,
        env=env,
        creationflags=CREATE_NEW_CONSOLE,
        startupinfo=info,
    )
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.kill()
        proc.wait()
        raise


def default_launcher(platform: str = sys.platform) -> Launcher | None:
    """None where relaybay cannot give the command a console of its own."""
    return launch_in_console if platform == "win32" else None

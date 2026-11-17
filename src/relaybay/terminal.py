"""Open a new terminal window that resumes an ended Claude Code session of a card: the fixed
command `claude --resume <id>` in the session's folder.

A security surface, kept narrow: the id is Claude Code's own session id checked against a strict
pattern, the folder must exist, and every command is an argument list, never a string a shell
reads. The platform and the PATH lookup are injectable, so tests never open a window.
"""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from relaybay.domain.errors import ConflictError
from relaybay.domain.sessions import resumable_id

Which = Callable[[str], str | None]
# Tried in this order on Linux and the BSDs; `x-terminal-emulator` is the system's choice.
LINUX_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")


@dataclass(frozen=True, slots=True)
class Launch:
    argv: list[str]
    cwd: Path
    # Windows without Windows Terminal: `claude` itself in a console window of its own.
    new_console: bool = False


# macOS: Terminal runs the line in the user's login shell, which may not be a POSIX one (fish
# reads backslashes inside single quotes). The folder, `claude` and the id come in as arguments,
# quoted by AppleScript, and a folder with a backslash or a single quote is refused.
MACOS_SCRIPT = (
    "on run argv",
    'tell application "Terminal"',
    'do script "cd " & quoted form of item 1 of argv & " && " & quoted form of item 2 of argv'
    ' & " --resume " & item 3 of argv',
    "activate",
    "end tell",
    "end run",
)


def child_environ(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The terminal's environment: the server's without Claude Code's own variables, so the
    resumed `claude` does not take itself for a child of another Claude Code session."""
    return {
        name: value
        for name, value in environ.items()
        if name != "CLAUDECODE" and not name.startswith("CLAUDE_CODE_")
    }


def resume_launch(
    cc_session_id: str | None, cwd: str | None, platform: str, which: Which
) -> Launch:
    """The command that opens a terminal running `claude --resume <cc_session_id>` in `cwd`;
    ConflictError with the reason when it cannot be built."""
    session_id = resumable_id(cc_session_id)
    if session_id is None:
        raise ConflictError("The session has no Claude Code session id to resume.")
    if not cwd or not cwd.isprintable():
        raise ConflictError("The session's folder is not known.")
    folder = Path(cwd)
    if not folder.is_absolute() or not folder.is_dir():
        raise ConflictError(f"The session's folder does not exist on this machine: {cwd}")
    claude = which("claude")
    if claude is None:
        raise ConflictError("`claude` is not on this machine's PATH.")
    command = [claude, "--resume", session_id]
    if platform == "win32":
        wt = which("wt")
        # Windows Terminal reads ';' in its command line as the start of its next command.
        if wt is not None and ";" not in cwd:
            return Launch([wt, "-d", str(folder), *command], folder)
        return Launch(command, folder, new_console=True)
    if platform == "darwin":
        osascript = which("osascript")
        if osascript is None:
            raise ConflictError("No terminal found: `osascript` is not on this machine's PATH.")
        if "\\" in cwd or "'" in cwd or "\\" in claude or "'" in claude:
            raise ConflictError(
                "On macOS a folder (or a `claude` path) with a backslash or a single quote is"
                f" not opened in Terminal: {cwd}"
            )
        lines = [part for line in MACOS_SCRIPT for part in ("-e", line)]
        return Launch([osascript, *lines, cwd, claude, session_id], folder)
    for name in LINUX_TERMINALS:
        path = which(name)
        if path is None:
            continue
        if name == "gnome-terminal":
            return Launch([path, f"--working-directory={folder}", "--", *command], folder)
        if name == "konsole":
            return Launch([path, "--workdir", str(folder), "-e", *command], folder)
        return Launch([path, "-e", *command], folder)
    raise ConflictError(f"No terminal found: none of {', '.join(LINUX_TERMINALS)} is on the PATH.")


def spawn(launch: Launch) -> None:
    """Start the terminal and leave it; relaybay does not wait for it or keep it. Every command
    is an argument list: no shell reads it."""
    env = child_environ()
    try:
        if launch.new_console:
            # The console window brings its own input and output: nothing is redirected.
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                launch.argv, cwd=launch.cwd, env=env, creationflags=flags, close_fds=True
            )
        else:
            subprocess.Popen(
                launch.argv,
                cwd=launch.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=sys.platform != "win32",
            )
    except OSError as exc:
        raise ConflictError(f"Could not start {Path(launch.argv[0]).name}: {exc}") from exc


@dataclass(slots=True)
class TerminalLauncher:
    platform: str = sys.platform
    which: Which = field(default=shutil.which)
    start: Callable[[Launch], None] = field(default=spawn)

    def open(self, cc_session_id: str | None, cwd: str | None) -> Launch:
        launch = resume_launch(cc_session_id, cwd, self.platform, self.which)
        self.start(launch)
        return launch

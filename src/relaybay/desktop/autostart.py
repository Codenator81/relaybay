"""Start the program at login, per user and without admin rights.

Windows: a value in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run. macOS: a LaunchAgent
plist with RunAtLoad. Linux: an XDG autostart `.desktop` file.
"""

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "relaybay"
AGENT_LABEL = "io.github.codenator81.relaybay"
DESKTOP_FILE = "relaybay.desktop"


def launch_command() -> list[str]:
    """How this program is started again, hidden: the frozen or `relaybay-desktop` executable,
    else this interpreter (`pythonw` on Windows, no console) with `-m relaybay.desktop`."""
    if getattr(sys, "frozen", False):
        # `relaybay desktop` from the console executable still registers the windowed sibling.
        name = "relaybay-desktop.exe" if sys.platform == "win32" else "relaybay-desktop"
        return [str(Path(sys.executable).with_name(name)), "--background"]
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if script is not None and script.stem == "relaybay-desktop" and script.exists():
        return [str(script.resolve()), "--background"]
    python = Path(sys.executable)
    if sys.platform == "win32":
        windowed = python.with_name("pythonw.exe")
        if windowed.exists():
            python = windowed
    return [str(python), "-m", "relaybay.desktop", "--background"]


def _desktop_exec(command: list[str]) -> str:
    """The Exec value of a desktop entry (Desktop Entry spec): `%` doubled (field codes),
    arguments with reserved characters double-quoted, with the double quote, backtick, dollar
    and backslash escaped by a backslash; then every backslash is doubled again by the string
    escaping of the file format."""
    parts = []
    for arg in command:
        arg = arg.replace("%", "%%")
        if arg and not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`"):
            parts.append(arg)
        else:
            escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
            parts.append(f'"{escaped}"')
    return " ".join(parts).replace("\\", "\\\\")


class Autostart:
    """The login entry of one platform. `registry` is the `winreg` module (a fake in tests),
    `home` the user's home folder."""

    def __init__(
        self,
        platform: str = sys.platform,
        home: Path | None = None,
        registry: Any = None,
        run_key: str = RUN_KEY,
    ) -> None:
        self.platform = platform
        self.home = home or Path.home()
        self.run_key = run_key
        self._registry = registry

    @property
    def registry(self) -> Any:
        if self._registry is None:
            import winreg

            self._registry = winreg
        return self._registry

    @property
    def path(self) -> Path | None:
        """The file that holds the entry (None on Windows)."""
        if self.platform == "win32":
            return None
        if self.platform == "darwin":
            return self.home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
        config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(config) if config else self.home / ".config"
        return base / "autostart" / DESKTOP_FILE

    def location(self) -> str:
        if self.platform == "win32":
            return f"HKCU\\{self.run_key}\\{VALUE_NAME}"
        return str(self.path)

    def enable(self, command: list[str]) -> None:
        if self.platform == "win32":
            reg = self.registry
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, self.run_key, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ, subprocess.list2cmdline(command))
            return
        path = self.path
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            data = {
                "Label": AGENT_LABEL,
                "ProgramArguments": command,
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }
            path.write_bytes(plistlib.dumps(data))
        else:
            lines = [
                "[Desktop Entry]",
                "Type=Application",
                "Name=relaybay",
                "Comment=Board and MCP server for Claude Code sessions",
                f"Exec={_desktop_exec(command)}",
                "Terminal=false",
                "X-GNOME-Autostart-enabled=true",
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def disable(self) -> None:
        if self.platform == "win32":
            reg = self.registry
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, self.run_key, 0, reg.KEY_SET_VALUE) as key:
                    reg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass  # no key or no value: already off
            return
        path = self.path
        assert path is not None
        path.unlink(missing_ok=True)

    def command(self) -> str | None:
        """The stored command line, or None when autostart is off."""
        if self.platform == "win32":
            reg = self.registry
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, self.run_key, 0, reg.KEY_READ) as key:
                    value, _kind = reg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                return None
            return str(value)
        path = self.path
        assert path is not None
        if not path.is_file():
            return None
        if self.platform == "darwin":
            return shlex.join(plistlib.loads(path.read_bytes())["ProgramArguments"])
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Exec="):
                return line[len("Exec=") :]
        return None

    def is_enabled(self) -> bool:
        return self.command() is not None

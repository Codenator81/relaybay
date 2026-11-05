"""How this relaybay was installed: the upgrade hint, the installer an update needs, the hook
executable of a frozen build, and whether that build runs from a place hooks can point at."""

import os
import sys
import tempfile
from pathlib import Path, PurePath

from relaybay import __version__

DESKTOP_KINDS = ("desktop-windows", "desktop-macos", "desktop-deb")
KINDS = (*DESKTOP_KINDS, "docker", "uv-tool", "pipx", "uvx", "source", "pip")


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_kind() -> str:
    """One of `KINDS`. A frozen build is the desktop program of its OS; the Docker image sets
    `RELAYBAY_DISTRIBUTION=docker`; the rest is told by where the environment lives."""
    if frozen():
        if sys.platform == "win32":
            return "desktop-windows"
        return "desktop-macos" if sys.platform == "darwin" else "desktop-deb"
    if os.environ.get("RELAYBAY_DISTRIBUTION") == "docker":
        return "docker"
    return _kind_of_prefix(Path(sys.prefix), Path(__file__))


def _kind_of_prefix(prefix: Path, module: Path) -> str:
    parts = [p.lower() for p in prefix.parts]
    if "uv" in parts and prefix.parent.name.lower() == "tools":
        return "uv-tool"
    if "pipx" in parts and prefix.parent.name.lower() == "venvs":
        return "pipx"
    # uvx runs from an environment in uv's cache (archive-v0/<hash>), gone after a cache clean.
    if "uv" in parts and any(p.startswith(("archive-v", "builds-v")) for p in parts):
        return "uvx"
    # src/relaybay/install.py inside a checkout: an editable install or `uv run`.
    if (module.resolve().parents[2] / "pyproject.toml").is_file():
        return "source"
    return "pip"


def hook_executable() -> Path | None:
    """The frozen `relaybay-hook` next to the running executable; None outside a frozen build
    (hooks then run `python -I -m relaybay.hook_client`)."""
    if not frozen():
        return None
    name = "relaybay-hook.exe" if sys.platform == "win32" else "relaybay-hook"
    return Path(sys.executable).with_name(name)


def install_path() -> Path:
    """The folder of the frozen program, else the Python environment relaybay runs in."""
    return Path(sys.executable).parent if frozen() else Path(sys.prefix)


def location_problem(
    executable: str | None = None,
    *,
    platform: str = sys.platform,
    temp: str | None = None,
    home: Path | None = None,
) -> str | None:
    """Why hooks must not point at this frozen program yet, or None. A macOS bundle run from the
    mounted `.dmg` (`/Volumes/`) or from Gatekeeper's random copy (`AppTranslocation`) disappears
    after the run; a program run from the temp folder or Downloads (an unpacked build artifact)
    is replaced or deleted later. Outside a frozen build there is nothing to check."""
    if executable is None:
        if not frozen():
            return None
        executable = sys.executable
    if platform == "darwin":
        path = executable.replace("\\", "/")
        if path.startswith("/Volumes/") or "/AppTranslocation/" in path:
            return (
                f"relaybay runs from {executable}, which goes away after this run: move"
                " relaybay.app to /Applications first, start it from there and run setup again"
            )
        return None
    where = PurePath(executable)
    folders = [Path(temp or tempfile.gettempdir()), (home or Path.home()) / "Downloads"]
    for folder in folders:
        if _inside(where, folder, platform):
            fix = (
                "install it with relaybay-<version>-windows-x64-setup.exe and start it from the"
                " Start menu"
                if platform == "win32"
                else "install the .deb package (it goes to /opt/relaybay)"
            )
            return (
                f"relaybay runs from {executable}, a temporary place: {fix}, then run setup again"
            )
    return None


def _inside(path: PurePath, folder: Path, platform: str) -> bool:
    def norm(value: PurePath) -> str:
        text = str(value)
        if platform == sys.platform:
            # Both sides real: %TEMP% often holds an 8.3 short name (C:\Users\LONGUS~1\...) while
            # sys.executable has the long one; symlinks (/tmp on macOS) likewise.
            text = os.path.realpath(text)
        text = text.replace("\\", "/").rstrip("/")
        return text.lower() if platform in ("win32", "darwin") else text

    return norm(path).startswith(norm(folder) + "/")


def stable_location() -> bool:
    return location_problem() is None


def where() -> str:
    """One line for `doctor`: `0.6.0 (desktop-windows: C:\\...\\relaybay.exe)`."""
    return f"{__version__} ({install_kind()}: {sys.executable})"

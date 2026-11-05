"""How this relaybay was installed: the upgrade hint, the installer an update needs, the hook
executable of a frozen build."""

import os
import sys
from pathlib import Path

DESKTOP_KINDS = ("desktop-windows", "desktop-macos", "desktop-deb")
KINDS = (*DESKTOP_KINDS, "docker", "uv-tool", "pipx", "uvx", "source", "pip")


def install_kind() -> str:
    """One of `KINDS`. A frozen build is the desktop program of its OS; the Docker image sets
    `RELAYBAY_DISTRIBUTION=docker`; the rest is told by where the environment lives."""
    if getattr(sys, "frozen", False):
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
    if not getattr(sys, "frozen", False):
        return None
    name = "relaybay-hook.exe" if sys.platform == "win32" else "relaybay-hook"
    return Path(sys.executable).with_name(name)

"""Autostart writers against a temporary home and a fake (or temporary) registry key."""

import plistlib
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from relaybay.desktop.autostart import AGENT_LABEL, Autostart, launch_command

COMMAND = ["C:/Program Files/relaybay/relaybay-desktop.exe", "--background"]


class FakeKey:
    def __init__(self, store: dict[str, dict[str, str]], path: str) -> None:
        self.values = store.setdefault(path, {})

    def __enter__(self) -> "FakeKey":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def fake_winreg() -> SimpleNamespace:
    store: dict[str, dict[str, str]] = {}

    def open_key(root: object, path: str, reserved: int = 0, access: int = 0) -> FakeKey:
        if path not in store:
            raise FileNotFoundError(path)
        return FakeKey(store, path)

    def query(key: FakeKey, name: str) -> tuple[str, int]:
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1

    def delete(key: FakeKey, name: str) -> None:
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]

    return SimpleNamespace(
        store=store,
        HKEY_CURRENT_USER=object(),
        KEY_SET_VALUE=2,
        KEY_READ=1,
        REG_SZ=1,
        CreateKeyEx=lambda root, path, reserved, access: FakeKey(store, path),
        OpenKey=open_key,
        SetValueEx=lambda key, name, reserved, kind, value: key.values.__setitem__(name, value),
        QueryValueEx=query,
        DeleteValue=delete,
    )


def test_windows_run_value_with_fake_registry(tmp_path: Path) -> None:
    reg = fake_winreg()
    entry = Autostart("win32", tmp_path, registry=reg)
    assert not entry.is_enabled()
    entry.disable()  # off already: no error
    entry.enable(COMMAND)
    stored = reg.store[r"Software\Microsoft\Windows\CurrentVersion\Run"]["relaybay"]
    assert stored == '"C:/Program Files/relaybay/relaybay-desktop.exe" --background'
    assert entry.command() == stored
    entry.enable([*COMMAND[:1], "--background"])  # rewriting keeps one value
    entry.disable()
    assert not entry.is_enabled()


@pytest.mark.skipif(sys.platform != "win32", reason="real registry, Windows only")
def test_windows_run_value_in_temporary_key(tmp_path: Path) -> None:
    import winreg

    key = rf"Software\relaybay-tests-{uuid.uuid4().hex}\Run"
    entry = Autostart("win32", tmp_path, run_key=key)
    try:
        entry.enable(COMMAND)
        assert entry.command() == '"C:/Program Files/relaybay/relaybay-desktop.exe" --background'
        entry.disable()
        assert entry.command() is None
    finally:
        parent = key.rsplit("\\", 1)[0]
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)


def test_macos_launch_agent(tmp_path: Path) -> None:
    entry = Autostart("darwin", tmp_path)
    entry.enable(["/Applications/relaybay.app/Contents/MacOS/relaybay-desktop", "--background"])
    path = tmp_path / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == AGENT_LABEL
    assert data["RunAtLoad"] is True
    assert data["ProgramArguments"][-1] == "--background"
    assert entry.is_enabled()
    entry.disable()
    assert not path.exists()


def test_linux_autostart_desktop_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    entry = Autostart("linux", tmp_path)
    entry.enable(["/opt/my apps/relaybay-desktop", "--background"])
    path = tmp_path / ".config" / "autostart" / "relaybay.desktop"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n")
    assert 'Exec="/opt/my apps/relaybay-desktop" --background\n' in text
    assert "Type=Application" in text
    assert entry.command() == '"/opt/my apps/relaybay-desktop" --background'
    entry.disable()
    assert not entry.is_enabled()


def test_linux_exec_escapes_percent_and_backslashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    entry = Autostart("linux", tmp_path)
    entry.enable(["/opt/rb/relaybay-desktop", "--name=50%", r"/tmp/a\b", "--background"])
    # % doubled; the backslash escaped inside the quotes, then doubled again by the file's
    # string escaping: one backslash becomes four.
    assert entry.command() == r'/opt/rb/relaybay-desktop --name=50%% "/tmp/a\\\\b" --background'


def test_linux_respects_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    entry = Autostart("linux", tmp_path)
    assert entry.path == tmp_path / "xdg" / "autostart" / "relaybay.desktop"


def test_launch_command_frozen_is_the_windowed_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    suffix = ".exe" if sys.platform == "win32" else ""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / f"relaybay{suffix}"))
    assert launch_command() == [str(tmp_path / f"relaybay-desktop{suffix}"), "--background"]


def test_launch_command_from_a_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["-c"])
    command = launch_command()
    assert command[-3:] == ["-m", "relaybay.desktop", "--background"]
    if sys.platform == "win32" and Path(command[0]).name.lower() == "pythonw.exe":
        assert Path(command[0]).exists()  # no console window at login

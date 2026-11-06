"""The update check (a fake opener, never the network), the installer download and its SHA-256
check, the upgrade hint per install kind, the server's loop and Inbox line, the tray model."""

import asyncio
import hashlib
import io
import json
import threading
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from starlette.testclient import TestClient

from relaybay import updates
from relaybay.app import create_app
from relaybay.config import load_settings
from relaybay.desktop.app import DesktopController
from relaybay.desktop.tray import MenuState, build_menu
from relaybay.install import _kind_of_prefix
from relaybay.service import Service
from relaybay.updates import Asset, Release, UpdateError, UpdateStatus

DL = updates.DOWNLOAD_PREFIX
PAGE = f"{updates.RELEASES_URL}/tag/v0.7.0"


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.url = url
        self.headers = headers or {}

    def geturl(self) -> str:
        return self.url


class FakeOpener:
    """Answers by URL: bytes, an exception to raise, or a (bytes, final URL) pair."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str], float]] = []

    def open(self, request: Any, timeout: float) -> Response:
        self.requests.append((request.full_url, dict(request.header_items()), timeout))
        answer = self.routes[request.full_url]
        if isinstance(answer, Exception):
            raise answer
        body, final = answer if isinstance(answer, tuple) else (answer, request.full_url)
        return Response(body, final)


def release_json(tag: str = "v0.7.0", **extra: Any) -> bytes:
    data = {
        "tag_name": tag,
        "html_url": PAGE,
        "assets": [
            {
                "name": "relaybay-0.7.0-windows-x64-setup.exe",
                "browser_download_url": f"{DL}v0.7.0/relaybay-0.7.0-windows-x64-setup.exe",
                "size": 10,
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": f"{DL}v0.7.0/SHA256SUMS.txt",
                "size": 100,
            },
        ],
    }
    return json.dumps(data | extra).encode()


def latest(body: bytes) -> FakeOpener:
    return FakeOpener({updates.LATEST_URL: body})


# --- the check ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "available"), [("0.6.0", True), ("0.7.0", False), ("0.8.1", False)]
)
def test_check_newer_same_older(current: str, available: bool) -> None:
    opener = latest(release_json())
    status = updates.check(UpdateStatus(current=current), opener)
    assert status.error is None and status.checked_at is not None
    assert status.latest is not None and status.latest.version == "0.7.0"
    assert status.latest.url == PAGE
    assert status.available is available
    url, headers, timeout = opener.requests[0]
    assert url == "https://api.github.com/repos/Codenator81/relaybay/releases/latest"
    assert headers["User-agent"].startswith("relaybay/")
    assert "Authorization" not in headers and timeout == 5


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", release_json(tag="latest"), release_json(tag=None)],
)
def test_malformed_answers_are_errors(body: bytes) -> None:
    with pytest.raises(UpdateError):
        updates.fetch_latest(latest(body))


def test_network_error_is_kept_with_the_last_release() -> None:
    status = updates.check(UpdateStatus(current="0.6.0"), latest(release_json()))
    status = updates.check(status, latest(URLError("offline")))  # type: ignore[arg-type]
    assert status.error and "offline" in status.error
    assert status.latest is not None and status.available  # the last answer stays


def test_foreign_links_are_dropped() -> None:
    body = release_json(html_url="javascript:alert(1)")
    data = json.loads(body)
    data["assets"][0]["browser_download_url"] = "https://evil.example/setup.exe"
    release = updates.fetch_latest(latest(json.dumps(data).encode()))
    assert release.url == updates.RELEASES_URL
    assert [a.name for a in release.assets] == ["SHA256SUMS.txt"]


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        ("0.6.0", "0.5.0", True),
        ("v0.10.0", "0.9.9", True),
        ("0.6.0", "0.6.0rc1", True),
        ("0.6.0rc2", "0.6.0rc1", True),
        ("0.6.0b1", "0.6.0", False),
        ("0.5.9", "0.6.0", False),
        ("0.6", "0.5.0", False),
        ("0.6.0", "0.0.0", True),
    ],
)
def test_is_newer(new: str, old: str, expected: bool) -> None:
    assert updates.is_newer(new, old) is expected


# --- installers ---------------------------------------------------------------

V = "0.7.0"


@pytest.mark.parametrize(
    ("kind", "arch", "name"),
    [
        ("desktop-windows", "x64", f"relaybay-{V}-windows-x64-setup.exe"),
        ("desktop-macos", "arm64", f"relaybay-{V}-macos-arm64.dmg"),
        ("desktop-deb", "x64", f"relaybay_{V}_amd64.deb"),
        ("desktop-windows", "arm64", None),
        ("desktop-macos", "x64", None),
        ("uv-tool", "x64", None),
        ("docker", "x64", None),
    ],
)
def test_asset_per_kind_and_arch(kind: str, arch: str, name: str | None) -> None:
    assert updates.asset_name(kind, arch, V) == name
    names = [
        f"relaybay-{V}-windows-x64-setup.exe",
        f"relaybay-{V}-macos-arm64.dmg",
        f"relaybay_{V}_amd64.deb",
    ]
    release = Release(V, PAGE, tuple(Asset(n, DL + n, 1) for n in names))
    found = updates.asset_for(release, kind, arch)
    assert (found.name if found else None) == name


def installer_release(payload: bytes, sums: str) -> tuple[Release, FakeOpener]:
    name = f"relaybay-{V}-windows-x64-setup.exe"
    release = Release(
        V,
        PAGE,
        (Asset(name, f"{DL}v{V}/{name}", len(payload)), Asset("SHA256SUMS.txt", DL + "sums", 1)),
    )
    opener = FakeOpener(
        {
            f"{DL}v{V}/{name}": (payload, "https://objects.githubusercontent.com/x"),
            DL + "sums": sums.encode(),
        }
    )
    return release, opener


def test_verified_installer_is_kept(tmp_path: Path) -> None:
    payload = b"installer bytes"
    sums = f"{hashlib.sha256(b'other').hexdigest()}  relaybay-0.7.0.tar.gz\n"
    sums += f"{hashlib.sha256(payload).hexdigest()}  relaybay-{V}-windows-x64-setup.exe\n"
    release, opener = installer_release(payload, sums)
    (tmp_path / "relaybay-0.6.9-windows-x64-setup.exe").write_bytes(b"an earlier download")
    path = updates.prepare_installer(release, "desktop-windows", "x64", tmp_path, opener)
    assert path.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [path]  # the earlier download is cleaned


@pytest.mark.parametrize(
    "sums",
    [
        f"{'0' * 64}  relaybay-{V}-windows-x64-setup.exe\n",  # mismatch
        f"{'0' * 64}  relaybay_{V}_amd64.deb\n",  # not listed
    ],
)
def test_installer_failing_the_check_is_refused_and_deleted(tmp_path: Path, sums: str) -> None:
    release, opener = installer_release(b"tampered", sums)
    with pytest.raises(UpdateError):
        updates.prepare_installer(release, "desktop-windows", "x64", tmp_path, opener)
    assert list(tmp_path.iterdir()) == []


def test_no_sums_no_install(tmp_path: Path) -> None:
    name = f"relaybay-{V}-windows-x64-setup.exe"
    release = Release(V, PAGE, (Asset(name, DL + name, 5),))
    with pytest.raises(UpdateError, match="SHA256SUMS"):
        updates.prepare_installer(release, "desktop-windows", "x64", tmp_path, FakeOpener({}))


def test_download_limits(tmp_path: Path) -> None:
    url = DL + "big"
    with pytest.raises(UpdateError, match="larger"):
        updates.download(url, tmp_path / "big", opener=FakeOpener({url: b"x" * 50}), max_bytes=10)
    with pytest.raises(UpdateError, match="left HTTPS"):
        opener = FakeOpener({url: (b"x", "http://example.com/big")})
        updates.download(url, tmp_path / "big", opener=opener)
    with pytest.raises(UpdateError, match="refusing"):
        updates.download("https://example.com/x", tmp_path / "x", opener=FakeOpener({}))
    assert list(tmp_path.iterdir()) == []


def test_sums_formats() -> None:
    text = f"{'A' * 64}  one.exe\n{'b' * 64} *two.deb\ngarbage\n"
    assert updates.parse_sums(text) == {"one.exe": "a" * 64, "two.deb": "b" * 64}


def test_installer_commands(tmp_path: Path) -> None:
    exe = tmp_path / "setup.exe"
    assert updates.installer_command(exe, "desktop-windows") == [
        str(exe),
        "/SILENT",
        "/CURRENTUSER",
    ]
    assert updates.installer_command(exe, "desktop-macos") == ["open", str(exe)]
    assert updates.installer_command(exe, "desktop-deb")[1:] == [str(exe)]
    with pytest.raises(UpdateError):
        updates.installer_command(exe, "pip")


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        ("uv-tool", "uv tool upgrade relaybay"),
        ("pipx", "pipx upgrade relaybay"),
        ("pip", "pip install -U relaybay"),
        ("uvx", "uvx relaybay@latest"),
        ("docker", "docker pull ghcr.io/codenator81/relaybay:0.7.0"),
        ("source", "git pull && uv sync"),
        ("desktop-windows", None),
    ],
)
def test_upgrade_hint(kind: str, hint: str | None) -> None:
    assert updates.upgrade_hint(kind, "0.7.0") == hint


@pytest.mark.parametrize(
    ("prefix", "kind"),
    [
        ("/home/a/.local/share/uv/tools/relaybay", "uv-tool"),
        ("C:/Users/a/AppData/Roaming/uv/data/tools/relaybay", "uv-tool"),
        ("/home/a/.local/share/pipx/venvs/relaybay", "pipx"),
        ("/home/a/.cache/uv/archive-v0/AbC123", "uvx"),
        ("/usr/local", "pip"),
    ],
)
def test_install_kind_from_the_prefix(tmp_path: Path, prefix: str, kind: str) -> None:
    module = tmp_path / "site-packages" / "relaybay" / "install.py"
    assert _kind_of_prefix(Path(prefix), module) == kind


def test_install_kind_of_a_checkout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    module = tmp_path / "src" / "relaybay" / "install.py"
    assert _kind_of_prefix(tmp_path / ".venv", module) == "source"


# --- the server: loop, Inbox, /update -------------------------------------------


async def test_update_loop_checks_after_the_delay_and_then_periodically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = latest(release_json())
    monkeypatch.setattr(updates, "default_opener", lambda: opener)
    service = Service(tmp_path / "relaybay.sqlite")
    loop = asyncio.create_task(service.update_loop(hours=0.05 / 3600, delay=0.1))
    await asyncio.sleep(0.05)
    assert opener.requests == []  # still in the start-up delay
    await asyncio.sleep(0.25)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
    assert len(opener.requests) >= 2
    assert service.update.latest is not None and service.update.latest.version == "0.7.0"


async def test_failed_check_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "default_opener", lambda: latest(OSError("down")))  # type: ignore[arg-type]
    service = Service(tmp_path / "relaybay.sqlite")
    status = await service.check_update()
    assert status.error and not status.available
    assert service.update_notice() is None


@pytest.mark.parametrize("on", [True, False])
def test_update_check_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    started: list[float] = []

    async def fake_loop(self: Service, hours: float, delay: float = 0) -> None:
        started.append(hours)

    monkeypatch.setattr(Service, "update_loop", fake_loop)
    settings = load_settings(home=tmp_path, environ={"RELAYBAY_UPDATE_CHECK": str(on).lower()})
    settings.update_check_hours = 6
    with TestClient(create_app(settings)):
        pass
    assert started == ([6] if on else [])


def test_update_check_settings_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="update_check_hours"):
        load_settings(home=tmp_path, environ={"RELAYBAY_UPDATE_CHECK_HOURS": "0"})
    with pytest.raises(ValueError, match="update_check must be true or false"):
        load_settings(home=tmp_path, environ={"RELAYBAY_UPDATE_CHECK": "sometimes"})


def test_inbox_line_and_update_json(board: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service: Service = board.app.state.service  # type: ignore[attr-defined]
    page = board.get("/inbox").text
    assert "available</a>" not in page
    monkeypatch.setattr("relaybay.service.install_kind", lambda: "uv-tool")
    service.update.latest = Release("99.0.0", f"{updates.RELEASES_URL}/tag/v99.0.0")
    page = board.get("/inbox").text
    assert "relaybay 99.0.0 available</a>" in page
    assert f'href="{updates.RELEASES_URL}/tag/v99.0.0"' in page
    assert "<code>uv tool upgrade relaybay</code>" in page
    assert board.get("/fragments/inbox-badge").text.count("99") == 0  # not counted
    monkeypatch.setattr("relaybay.service.install_kind", lambda: "desktop-windows")
    page = board.get("/inbox").text
    assert "<em>Update to 99.0.0</em> in the relaybay tray menu, or Settings" in page
    assert "Check for updates" in page
    data = board.get("/update").json()
    assert data["latest"] == "99.0.0" and data["available"] is True
    assert data["command"] is None


def test_update_json_needs_sign_in(client: TestClient) -> None:
    assert client.get("/update").status_code == 401


# --- the program: tray model and the Update action --------------------------------


def test_menu_with_an_update() -> None:
    entries = build_menu(
        MenuState("embedded", autostart=False, has_window=True, update_version="0.7.0")
    )
    texts = [e.text for e in entries]
    assert texts[-3:] == ["Update to 0.7.0", "-", "Quit relaybay"]
    update = entries[-3]
    assert update.action == "install_update" and hasattr(DesktopController, update.action)


def controller_with_update(tmp_path: Path) -> tuple[DesktopController, list[str]]:
    controller = DesktopController(tmp_path)
    controller.update = UpdateStatus(current="0.6.0", latest=Release(V, PAGE))
    said: list[str] = []
    controller.notify = said.append  # type: ignore[method-assign]
    controller.show_problem = lambda message, ask_settings=False: said.append(message)  # type: ignore[method-assign]
    return controller, said


def test_update_action_outside_the_installed_program_shows_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, said = controller_with_update(tmp_path)
    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "pipx")
    controller.install_update()
    assert said == [f"relaybay {V} is available; upgrade with: pipx upgrade relaybay"]
    assert controller.menu_entries()[-3].text == f"Update to {V}"


def test_update_action_never_runs_a_failed_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, said = controller_with_update(tmp_path)
    launched: list[Path] = []
    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "desktop-deb")
    monkeypatch.setattr(
        "relaybay.desktop.app.platformdirs.user_cache_dir", lambda *a, **k: tmp_path
    )

    def refused(*args: Any, **kwargs: Any) -> Path:
        raise UpdateError("SHA-256 does not match")

    monkeypatch.setattr(updates, "prepare_installer", refused)
    monkeypatch.setattr(updates, "launch_installer", lambda path, kind: launched.append(path))
    controller.install_update()
    assert launched == [] and not controller.quitting
    assert said[-1] == f"relaybay {V} was not installed: SHA-256 does not match"


def test_update_action_starts_a_verified_installer_and_quits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _said = controller_with_update(tmp_path)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "desktop-macos")
    monkeypatch.setattr(
        "relaybay.desktop.app.platformdirs.user_cache_dir", lambda *a, **k: tmp_path
    )
    monkeypatch.setattr(updates, "prepare_installer", lambda *a, **k: tmp_path / "x.dmg")
    monkeypatch.setattr(
        updates, "launch_installer", lambda path, kind: launched.append((path, kind))
    )
    controller.install_update()
    assert launched == [(tmp_path / "x.dmg", "desktop-macos")]
    assert controller.quitting and controller.installer_started_at is not None


def test_an_installer_that_does_not_start_is_not_an_update_quit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _said = controller_with_update(tmp_path)
    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "desktop-windows")
    monkeypatch.setattr(
        "relaybay.desktop.app.platformdirs.user_cache_dir", lambda *a, **k: tmp_path
    )
    monkeypatch.setattr(updates, "prepare_installer", lambda *a, **k: tmp_path / "x.exe")

    def refused(path: Path, kind: str) -> None:
        raise OSError("blocked")

    monkeypatch.setattr(updates, "launch_installer", refused)
    with pytest.raises(OSError, match="blocked"):
        controller.install_update()
    assert controller.installer_started_at is None and not controller.quitting


def test_program_check_now_reports_and_marks_the_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, said = controller_with_update(tmp_path)
    controller.update = UpdateStatus(current="0.6.0")
    monkeypatch.setattr(updates, "default_opener", lambda: latest(release_json()))
    controller.check_updates()
    assert said == ["relaybay 0.7.0 is available: tray menu, Update."]
    assert controller.menu_entries()[-3].action == "install_update"


def test_tray_icon_dot() -> None:
    pytest.importorskip("PIL", reason="the desktop extra")
    from relaybay.desktop.tray import icon_image

    plain, marked = icon_image(), icon_image(update=True)
    # The middle of the dot: its outline covers other pixels at the 16 px icon's scale.
    dot = max(min(marked.size) * 3 // 8, 6)
    corner = (marked.width - 1 - dot // 2, marked.height - 1 - dot // 2)
    assert marked.convert("RGB").getpixel(corner) == (0xC7, 0x77, 0x00)
    assert plain.convert("RGBA").getpixel(corner) != marked.getpixel(corner)


def test_settings_type_round_trip(tmp_path: Path) -> None:
    from relaybay.desktop.settings import (
        DesktopSettings,
        load_desktop_settings,
        save_desktop_settings,
        settings_path,
    )

    save_desktop_settings(tmp_path, DesktopSettings(update_check=False, update_check_hours=12))
    assert "update_check_hours = 12\n" in settings_path(tmp_path).read_text(encoding="utf-8")
    loaded = load_desktop_settings(tmp_path)
    assert loaded.update_check is False and loaded.update_check_hours == 12
    settings_path(tmp_path).write_text("update_check_hours = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="update_check_hours"):
        load_desktop_settings(tmp_path)


# --- review fixes ----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [json.dumps({"tag_name": "v9.0.0", "assets": 5}).encode(), b"[" * 100000],
)
def test_odd_answers_become_an_error_not_an_exception(body: bytes) -> None:
    status = updates.check(UpdateStatus(current="0.6.0"), latest(body))
    assert status.error and status.latest is None


@pytest.mark.parametrize(
    "environ", [{"RELAYBAY_UPDATE_CHECK_HOURS": "721"}, {"RELAYBAY_UPDATE_CHECK_HOURS": "0"}]
)
def test_update_hours_have_bounds(tmp_path: Path, environ: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="from 1 to 720"):
        load_settings(home=tmp_path, environ=environ)
    from relaybay.desktop.settings import load_desktop_settings, settings_path

    settings_path(tmp_path).write_text(
        f"update_check_hours = {environ['RELAYBAY_UPDATE_CHECK_HOURS']}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="from 1 to 720"):
        load_desktop_settings(tmp_path)


def test_desktop_loop_survives_a_failure_and_caps_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    controller = DesktopController(tmp_path)
    controller.desktop.update_check_hours = 10**6  # past validation: the cap still holds
    calls: list[int] = []
    waits: list[float] = []

    def boom() -> None:
        calls.append(1)
        raise TypeError("unexpected")

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) > 2

    monkeypatch.setattr(controller, "_check_update", boom)
    monkeypatch.setattr(controller._stopped, "wait", wait)
    controller.update_loop()
    assert calls == [1, 1]  # the loop went on after the first failure
    assert waits[0] == updates.START_DELAY
    # The cap is the platform's: Windows allows about 49 days, POSIX far more than the hours set.
    capped = min(10**6 * 3600, threading.TIMEOUT_MAX)
    assert waits[1:] == [capped, capped]


def test_defaults_are_not_written_so_an_older_program_reads_the_file(tmp_path: Path) -> None:
    from relaybay.desktop.settings import DesktopSettings, save_desktop_settings, settings_path

    save_desktop_settings(tmp_path, DesktopSettings(autostart=True))
    text = settings_path(tmp_path).read_text(encoding="utf-8")
    assert "update_check" not in text  # 0.5 refuses unknown keys
    save_desktop_settings(tmp_path, DesktopSettings(update_check=False))
    assert "update_check = false" in settings_path(tmp_path).read_text(encoding="utf-8")


def test_second_update_click_is_ignored_while_one_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    controller, said = controller_with_update(tmp_path)
    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "desktop-deb")
    monkeypatch.setattr(
        "relaybay.desktop.app.platformdirs.user_cache_dir", lambda *a, **k: tmp_path
    )
    entered, release = threading.Event(), threading.Event()
    downloads: list[int] = []

    def slow(*args: Any, **kwargs: Any) -> Path:
        downloads.append(1)
        entered.set()
        release.wait(5)
        raise UpdateError("network down")

    monkeypatch.setattr(updates, "prepare_installer", slow)
    first = threading.Thread(target=controller.install_update)
    first.start()
    assert entered.wait(5)
    controller.install_update()  # the second click
    release.set()
    first.join(5)
    assert downloads == [1]
    assert "The update is already running." in said
    controller.install_update()  # after a failure the next click tries again
    assert downloads == [1, 1]


def test_windows_setup_runs_one_at_a_time() -> None:
    iss = Path(__file__).parents[1] / "packaging" / "windows" / "relaybay.iss"
    assert "SetupMutex=relaybay-setup" in iss.read_text(encoding="utf-8")


class FakeInstaller:
    def __init__(self) -> None:
        self.exit = threading.Event()

    def wait(self) -> int:
        self.exit.wait(10)
        return 1


def windows_update_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DesktopController, list[str], list[FakeInstaller]]:
    controller, said = controller_with_update(tmp_path)
    installers: list[FakeInstaller] = []

    def launched(path: Path, kind: str) -> FakeInstaller:
        installers.append(FakeInstaller())
        return installers[-1]

    monkeypatch.setattr("relaybay.desktop.app.install_kind", lambda: "desktop-windows")
    monkeypatch.setattr(
        "relaybay.desktop.app.platformdirs.user_cache_dir", lambda *a, **k: tmp_path
    )
    monkeypatch.setattr(updates, "prepare_installer", lambda *a, **k: tmp_path / "x.exe")
    monkeypatch.setattr(updates, "launch_installer", launched)
    controller.install_update()
    return controller, said, installers


def installer_thread() -> threading.Thread:
    return next(t for t in threading.enumerate() if t.name == "relaybay-installer")


def test_update_works_again_after_a_cancelled_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, said, installers = windows_update_started(tmp_path, monkeypatch)
    watcher = installer_thread()
    controller.install_update()
    assert said[-1] == "The update is already running." and len(installers) == 1

    installers[0].exit.set()
    watcher.join(10)
    assert controller.installer_started_at is None and not controller.quitting
    controller.install_update()
    assert len(installers) == 2
    second = installer_thread()
    installers[1].exit.set()
    second.join(10)


def test_a_setup_that_asked_for_the_quit_keeps_the_update_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _said, installers = windows_update_started(tmp_path, monkeypatch)
    watcher = installer_thread()
    controller.quitting = True
    installers[0].exit.set()
    watcher.join(10)
    assert controller._updating.locked() and controller.installer_started_at is not None

"""The update check and the installer download.

One unauthenticated GET of the latest GitHub release (no telemetry: only the `User-Agent` with the
version), through the system proxy. An installer is checked against `SHA256SUMS.txt` of the same
release before anything runs it: that is integrity (a broken or swapped download), not
authenticity, since nothing is signed.
"""

import hashlib
import http.client
import json
import logging
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from relaybay import __version__

log = logging.getLogger(__name__)

REPO = "Codenator81/relaybay"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
SUMS_FILE = "SHA256SUMS.txt"
TIMEOUT = 5.0
DOWNLOAD_TIMEOUT = 60.0
JSON_MAX = 1024 * 1024
SUMS_MAX = 1024 * 1024
DOWNLOAD_MAX = 200 * 1024 * 1024
CHUNK = 1024 * 1024
START_DELAY = 30.0  # seconds after start-up before the first check
HOURS_MAX = 720  # update_check_hours: at most once a month

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
_PRE = {"a": 0, "b": 1, "rc": 2}
_SUM_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")


class UpdateError(Exception):
    """The check or the download failed; the message is for the log, the tray or the form."""


class Opener(Protocol):
    def open(self, request: Request, timeout: float) -> Any: ...


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class Release:
    version: str  # without the leading "v"
    url: str  # the release page, with its notes
    assets: tuple[Asset, ...] = ()

    def asset(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)


@dataclass(frozen=True, slots=True)
class UpdateNotice:
    """The Inbox line: the version, its release page, the upgrade command (None: the desktop
    program, whose tray menu has Update)."""

    version: str
    url: str
    command: str | None


@dataclass(slots=True)
class UpdateStatus:
    """What the last check found; kept in memory only."""

    current: str = __version__
    latest: Release | None = None
    checked_at: datetime | None = None
    error: str | None = None
    enabled: bool = True

    @property
    def available(self) -> bool:
        return self.latest is not None and is_newer(self.latest.version, self.current)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest.version if self.latest else None,
            "available": self.available,
            "url": self.latest.url if self.latest else None,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "error": self.error,
            "enabled": self.enabled,
        }


def parse_version(text: str) -> tuple[int, ...] | None:
    """`X.Y.Z` with an optional `aN`, `bN` or `rcN` as a comparable tuple; None otherwise."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch, pre, number = match.groups()
    tail = (_PRE[pre], int(number)) if pre else (3, 0)  # a final release sorts after its pre
    return (int(major), int(minor), int(patch), *tail)


def is_newer(latest: str, current: str) -> bool:
    """PEP 440 order for the versions relaybay publishes; False when either is not one."""
    new, old = parse_version(latest), parse_version(current)
    return new is not None and old is not None and new > old


def default_opener() -> Opener:
    """urllib's opener: it follows the system proxy (environment, Windows registry, macOS)."""
    return build_opener()


def _get(url: str, opener: Opener | None, timeout: float, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": f"relaybay/{__version__}"})
    try:
        with (opener or default_opener()).open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise UpdateError(f"{urlsplit(url).netloc}: {exc}") from exc
    if len(data) > limit:
        raise UpdateError(f"{url}: the answer is larger than {limit} bytes")
    return data


def _release(data: Any) -> Release:
    if not isinstance(data, dict):
        raise UpdateError("the release answer is not a JSON object")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise UpdateError(f"the release tag {tag!r} is not a version")
    page = data.get("html_url")
    # Shown as a link on the board: only the project's own release pages.
    url = page if isinstance(page, str) and page.startswith(f"{RELEASES_URL}/") else RELEASES_URL
    items = data.get("assets", [])
    if not isinstance(items, list):
        raise UpdateError("the release's assets are not a list")
    assets = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name, link, size = item.get("name"), item.get("browser_download_url"), item.get("size")
        if (
            isinstance(name, str)
            and isinstance(link, str)
            and link.startswith(DOWNLOAD_PREFIX)
            and isinstance(size, int)
        ):
            assets.append(Asset(name, link, size))
    return Release(tag.strip().removeprefix("v"), url, tuple(assets))


def fetch_latest(opener: Opener | None = None, timeout: float = TIMEOUT) -> Release:
    """The latest published release (GitHub leaves drafts and pre-releases out)."""
    raw = _get(LATEST_URL, opener, timeout, JSON_MAX)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly deep nesting
        raise UpdateError(f"the release answer is not JSON: {exc}") from exc
    return _release(data)


def check(status: UpdateStatus, opener: Opener | None = None) -> UpdateStatus:
    """Run one check into `status`: the release, or the error with the last release kept."""
    try:
        status.latest = fetch_latest(opener)
        status.error = None
    except UpdateError as exc:
        status.error = str(exc)
    status.checked_at = datetime.now(UTC)
    return status


def machine_arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)


def asset_name(kind: str, arch: str, version: str) -> str | None:
    """The installer's file name for an install kind and CPU (the names `desktop.yml` builds);
    None where no installer is published."""
    names = {
        ("desktop-windows", "x64"): f"relaybay-{version}-windows-x64-setup.exe",
        ("desktop-macos", "arm64"): f"relaybay-{version}-macos-arm64.dmg",
        ("desktop-deb", "x64"): f"relaybay_{version}_amd64.deb",
    }
    return names.get((kind, arch))


def asset_for(release: Release, kind: str, arch: str) -> Asset | None:
    name = asset_name(kind, arch, release.version)
    return release.asset(name) if name else None


def upgrade_hint(kind: str, version: str) -> str | None:
    """The command that upgrades this install; None for the desktop program (its tray menu has
    Update)."""
    return {
        "uv-tool": "uv tool upgrade relaybay",
        "pipx": "pipx upgrade relaybay",
        "pip": "pip install -U relaybay",
        "uvx": "uvx relaybay@latest",
        "docker": f"docker pull ghcr.io/codenator81/relaybay:{version}",
        "source": "git pull && uv sync",
    }.get(kind)


def download(
    url: str, dest: Path, *, opener: Opener | None = None, max_bytes: int = DOWNLOAD_MAX
) -> Path:
    """Stream `url` to `dest` over HTTPS only, at most `max_bytes`; a partial file never stays."""
    if not url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError(f"refusing to download from {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = Request(url, headers={"User-Agent": f"relaybay/{__version__}"})
    try:
        with (opener or default_opener()).open(request, timeout=DOWNLOAD_TIMEOUT) as response:
            if not str(response.geturl()).startswith("https://"):
                raise UpdateError(f"the download of {dest.name} left HTTPS")
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise UpdateError(f"{dest.name} is larger than {max_bytes} bytes")
            total = 0
            with part.open("wb") as fh:
                while chunk := response.read(CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UpdateError(f"{dest.name} is larger than {max_bytes} bytes")
                    fh.write(chunk)
        part.replace(dest)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise UpdateError(f"the download of {dest.name} failed: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)
    return dest


def parse_sums(text: str) -> dict[str, str]:
    """`sha256sum` output (`<hash>  <name>` or `<hash> *<name>`) as name -> lower-case hash."""
    sums = {}
    for line in text.splitlines():
        match = _SUM_LINE.fullmatch(line.strip())
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, sums_text: str) -> None:
    """Raise UpdateError unless `SHA256SUMS.txt` lists `path`'s name with its hash."""
    expected = parse_sums(sums_text).get(path.name)
    if expected is None:
        raise UpdateError(f"{path.name} is not listed in {SUMS_FILE}")
    actual = sha256_of(path)
    if actual != expected:
        raise UpdateError(f"{path.name}: SHA-256 {actual} does not match {expected} in {SUMS_FILE}")


def clean_folder(folder: Path) -> None:
    """Delete the installers of earlier updates; one still in use (a setup that runs) stays."""
    for path in folder.iterdir() if folder.is_dir() else ():
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                log.warning("old download %s not deleted: %s", path, exc)


def prepare_installer(
    release: Release, kind: str, arch: str, folder: Path, opener: Opener | None = None
) -> Path:
    """Download this system's installer of `release` into `folder` and verify it; a file that
    fails the check is deleted. Returns the verified file."""
    asset = asset_for(release, kind, arch)
    if asset is None:
        raise UpdateError(
            f"relaybay {release.version} has no installer for this system ({kind}, {arch});"
            f" see {release.url}"
        )
    sums = release.asset(SUMS_FILE)
    if sums is None:
        raise UpdateError(f"relaybay {release.version} has no {SUMS_FILE}; nothing is installed")
    if asset.size > DOWNLOAD_MAX:
        raise UpdateError(f"{asset.name} is larger than {DOWNLOAD_MAX} bytes")
    clean_folder(folder)
    sums_text = _get(sums.url, opener, DOWNLOAD_TIMEOUT, SUMS_MAX).decode("utf-8", "replace")
    path = download(asset.url, folder / asset.name, opener=opener)
    try:
        verify_sha256(path, sums_text)
    except UpdateError:
        path.unlink(missing_ok=True)
        raise
    return path


def installer_command(path: Path, kind: str) -> list[str]:
    """What starts a verified installer: Inno Setup silently for this user, the disk image or
    the package with the OS's own opener."""
    if kind == "desktop-windows":
        return [str(path), "/SILENT", "/CURRENTUSER"]
    if kind == "desktop-macos":
        return ["open", str(path)]
    if kind == "desktop-deb":
        return [shutil.which("xdg-open") or "xdg-open", str(path)]
    raise UpdateError(f"no installer for {kind}")


def launch_installer(path: Path, kind: str) -> None:
    """Start the installer detached, so it outlives the program that quits for it."""
    command = installer_command(path, kind)
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(command, start_new_session=True, close_fds=True)

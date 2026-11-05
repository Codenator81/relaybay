"""`desktop.toml`: the desktop program's own settings, read by the CLI too.

It lives in the per-user config folder (`RELAYBAY_CONFIG_DIR` overrides it), not in the data
folder, because one of its keys says where the data folder is. The external server's token is
kept beside it in `external-token` (0600), never in the TOML.
"""

import json
import os
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import platformdirs

from relaybay.security import _write_private

SERVER_MODES = ("embedded", "external")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
FILE_NAME = "desktop.toml"
EXTERNAL_TOKEN = "external-token"


def _doc(meaning: str, default: str | None = None) -> dict[str, str | None]:
    """Field metadata for the configuration reference (`scripts/gen_reference.py`)."""
    return {"meaning": meaning, "default": default}


@dataclass(slots=True)
class DesktopSettings:
    data_dir: Path | None = field(
        default=None,
        metadata=_doc(
            "Data folder (an absolute path) for the program and the CLI when neither `--home`"
            " nor `RELAYBAY_HOME` is given. Pointing it at another folder moves nothing.",
            default="unset: the per-user data dir",
        ),
    )
    autostart: bool = field(
        default=False,
        metadata=_doc(
            "Start the program at login (hidden, tray icon only). Each start rewrites the entry"
            " with the current executable."
        ),
    )
    server_mode: str = field(
        default="embedded",
        metadata=_doc(
            "`embedded`: the program runs the server itself. `external`: it opens `server_url`"
            " with the token in `external-token`, and the CLI (`token`, `hook`, `setup`,"
            " `doctor`) talks to that server too; `relaybay serve` ignores it. Plain `http://`"
            " to another machine warns: the token travels in clear text."
        ),
    )
    server_url: str = field(
        default="",
        metadata=_doc("Base URL of the external server (`http://` or `https://`).", default="`''`"),
    )


def default_config_dir() -> Path:
    return Path(platformdirs.user_config_dir("relaybay", appauthor=False))


def config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    value = env.get("RELAYBAY_CONFIG_DIR")
    return Path(value).expanduser() if value else default_config_dir()


def settings_path(folder: Path) -> Path:
    return folder / FILE_NAME


def external_token_path(folder: Path) -> Path:
    return folder / EXTERNAL_TOKEN


def check_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"server_url must be an http:// or https:// address, got {value!r}")
    return url


def data_dir_path(value: str) -> Path:
    """An absolute folder: a relative one would depend on where a command was started."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"data_dir must be an absolute folder path, got {value!r}")
    return path


def cleartext_warning(settings: DesktopSettings) -> str | None:
    """A warning when an external server is reached over plain http on another machine."""
    if settings.server_mode != "external" or not settings.server_url:
        return None
    parts = urlsplit(settings.server_url)
    if parts.scheme == "http" and (parts.hostname or "") not in LOOPBACK:
        return (
            f"{settings.server_url}: the token travels in clear text; use https or an SSH tunnel."
        )
    return None


def load_desktop_settings(folder: Path, ignore_unknown: bool = False) -> DesktopSettings:
    """Read `desktop.toml` in `folder`; a missing file means the defaults. A bad value raises
    ValueError naming the file and the key; `ignore_unknown` skips keys of a newer version."""
    path = settings_path(folder)
    settings = DesktopSettings()
    if not path.is_file():
        return settings
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    known = {f.name for f in fields(DesktopSettings)}
    unknown = set(data) - known
    if unknown and not ignore_unknown:
        raise ValueError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")
    if "data_dir" in data:
        value = data["data_dir"]
        if not isinstance(value, str):
            raise ValueError(f"{path}: data_dir must be a folder path, got {value!r}")
        try:
            settings.data_dir = data_dir_path(value) if value.strip() else None
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None
    if "autostart" in data:
        if not isinstance(data["autostart"], bool):
            raise ValueError(f"{path}: autostart must be true or false, got {data['autostart']!r}")
        settings.autostart = data["autostart"]
    if "server_mode" in data:
        mode = str(data["server_mode"]).strip().lower()
        if mode not in SERVER_MODES:
            raise ValueError(
                f"{path}: server_mode must be one of {', '.join(SERVER_MODES)},"
                f" got {data['server_mode']!r}"
            )
        settings.server_mode = mode
    if data.get("server_url"):
        try:
            settings.server_url = check_url(str(data["server_url"]))
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None
    if settings.server_mode == "external" and not settings.server_url:
        raise ValueError(f"{path}: server_mode = 'external' needs server_url")
    return settings


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    # A JSON string with ASCII escapes is a valid TOML basic string.
    return json.dumps(str(value))


def save_desktop_settings(folder: Path, settings: DesktopSettings) -> Path:
    """Write `desktop.toml` atomically (temp file + rename); unset keys are left out."""
    lines = ["# relaybay desktop program settings (relaybay desktop config)"]
    for f in fields(DesktopSettings):
        value = getattr(settings, f.name)
        if value is None or value == "":
            continue
        lines.append(f"{f.name} = {_toml_value(value)}")
    path = settings_path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def write_external_token(folder: Path, token: str) -> Path:
    token = token.strip()
    if not token:
        raise ValueError("the external server token is empty")
    path = external_token_path(folder)
    _write_private(path, token)
    return path

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

from relaybay.domain.notifications import DEFAULT_KINDS, KINDS
from relaybay.security import _write_private
from relaybay.updates import HOURS_MAX

SERVER_MODES = ("embedded", "external")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
FILE_NAME = "desktop.toml"
EXTERNAL_TOKEN = "external-token"
# Keys added after 0.5.
OMIT_AT_DEFAULT = ("update_check", "update_check_hours", "notify", "notify_quiet", "notify_kinds")


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
    update_check: bool = field(
        default=True,
        metadata=_doc(
            "The program asks GitHub for the latest release shortly after the start and every"
            " `update_check_hours`; a newer one puts a dot on the tray icon and Update to X.Y.Z"
            " in its menu. Settings → Check for updates checks now either way. The server's own"
            " check is `update_check` in `config.toml`."
        ),
    )
    update_check_hours: int = field(
        default=24,
        metadata=_doc("Hours between the program's update checks (whole number from 1 to 720)."),
    )
    notify: bool = field(
        default=True,
        metadata=_doc(
            "Tray notifications: the program asks the server every 5 seconds what needs you and"
            " shows it, at most one notification per 20 seconds. Read at the start and on"
            " Restart server."
        ),
    )
    notify_quiet: bool = field(
        default=False,
        metadata=_doc(
            "Quiet mode (the tray's Quiet notifications): no notification, a dot on the tray icon,"
            " one summary on leaving it."
        ),
    )
    notify_kinds: tuple[str, ...] = field(
        default=DEFAULT_KINDS,
        metadata=_doc(
            "What to notify about, any of: `waiting` (a task now waits on you), `review` (a task"
            " moved to review or test), `reply` (an agent's comment), `answer` (an external"
            " message for a task that waits on someone), `question` (an agent's blocking"
            " question or look-at-this request), `approval` (an agent waits for your"
            " approval of a push, a merge or a stand change), `permission` (a session waiting for"
            " you, off by default), `brake` (an agent or a rule pulled the brake), `budget` (a"
            " token budget nearly or fully spent), `server` (an external server stopped"
            " answering).",
            default="all but `permission`",
        ),
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
    if "update_check" in data:
        if not isinstance(data["update_check"], bool):
            raise ValueError(
                f"{path}: update_check must be true or false, got {data['update_check']!r}"
            )
        settings.update_check = data["update_check"]
    if "update_check_hours" in data:
        hours = data["update_check_hours"]
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= HOURS_MAX:
            raise ValueError(
                f"{path}: update_check_hours must be a whole number from 1 to {HOURS_MAX},"
                f" got {hours!r}"
            )
        settings.update_check_hours = hours
    for key in ("notify", "notify_quiet"):
        if key in data:
            if not isinstance(data[key], bool):
                raise ValueError(f"{path}: {key} must be true or false, got {data[key]!r}")
            setattr(settings, key, data[key])
    if "notify_kinds" in data:
        settings.notify_kinds = _notify_kinds(path, data["notify_kinds"], ignore_unknown)
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


def _notify_kinds(path: Path, value: Any, ignore_unknown: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(kind, str) for kind in value):
        raise ValueError(f"{path}: notify_kinds must be a list of names, got {value!r}")
    unknown = sorted(set(value) - set(KINDS))
    if unknown and not ignore_unknown:
        raise ValueError(
            f"{path}: notify_kinds has unknown kind(s): {', '.join(unknown)};"
            f" known: {', '.join(KINDS)}"
        )
    return tuple(kind for kind in value if kind in KINDS)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, tuple):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    # A JSON string with ASCII escapes is a valid TOML basic string.
    return json.dumps(str(value))


def save_desktop_settings(folder: Path, settings: DesktopSettings) -> Path:
    """Write `desktop.toml` atomically (temp file + rename); unset keys are left out, and so are
    the update keys at their defaults, so an older program (which refuses unknown keys) still
    reads the file."""
    lines = ["# relaybay desktop program settings (relaybay desktop config)"]
    for f in fields(DesktopSettings):
        value = getattr(settings, f.name)
        if value is None or value == "":
            continue
        if f.name in OMIT_AT_DEFAULT and value == f.default:
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

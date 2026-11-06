"""The backup before a new version: the database and the settings files, taken before the
migration of a version the data folder has not run yet.

`<data>/last_version` records the version that last started cleanly; it is written only after
the migration. Backups go to `<data>/backups/<old>-to-<new>-<local time>/` (the same clock as the
log next to it); only folders with that name pattern are counted and rotated, anything else in the
data folder is left alone.
"""

import logging
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from relaybay import __version__
from relaybay.config import Settings

log = logging.getLogger(__name__)

VERSION_FILE = "last_version"
BACKUPS = "backups"
# PEP 440 public versions as relaybay publishes them; no hyphen, so "-to-" splits a name safely.
_VERSION = r"\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?"
_NAME = re.compile(rf"(unknown|{_VERSION})-to-({_VERSION})-(\d{{8}}-\d{{6}})(?:-(\d+))?")


def last_version(home: Path) -> str | None:
    path = home / VERSION_FILE
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def backup_due(settings: Settings, version: str = __version__) -> bool:
    """A database exists and was last run by another version (or by one before this file)."""
    return settings.db_path.is_file() and last_version(settings.home) != version


def _ours(folder: Path) -> list[tuple[tuple[str, int], str, str, Path]]:
    """Our backup folders, oldest first: ((stamp, suffix), old, new, path). Nothing else under
    `backups/` is listed, so it is never counted or deleted."""
    found = []
    for path in folder.iterdir() if folder.is_dir() else ():
        match = _NAME.fullmatch(path.name)
        if match and path.is_dir():
            old, new, stamp, suffix = match.groups()
            found.append(((stamp, int(suffix or 0)), old, new, path))
    return sorted(found)


def rotate(folder: Path, keep: int) -> list[Path]:
    """Delete our oldest backups beyond `keep`, one by one: a folder that cannot be deleted
    (a file held open on Windows) is logged and skipped. Returns the deleted folders."""
    ours = _ours(folder)
    deleted = []
    for _order, _old, _new, path in ours[: max(len(ours) - keep, 0)]:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            log.warning("old backup %s not deleted: %s", path, exc)
            continue
        deleted.append(path)
    return deleted


def _free(path: Path) -> Path:
    """`path`, or `path-2`, `path-3`... when something already has that name."""
    candidate, number = path, 1
    while candidate.exists():
        number += 1
        candidate = path.with_name(f"{path.name}-{number}")
    return candidate


def backup_before_upgrade(
    settings: Settings, version: str = __version__, now: datetime | None = None
) -> Path | None:
    """Copy the database (sqlite3 backup API: consistent with a live WAL) and `config.toml`,
    `policy.json`, `token` when a new version is about to migrate; None when not due,
    `backup_keep` is 0, or a backup of the same `<old>-to-<new>` step already exists (a start
    that failed before `last_version` was written: the first copy is the one to keep). A failed
    copy raises and leaves no partial folder; a failed rotation is only logged."""
    if settings.backup_keep < 1 or not backup_due(settings, version):
        return None
    recorded = last_version(settings.home)
    old = recorded if recorded and re.fullmatch(_VERSION, recorded) else "unknown"
    folder = settings.home / BACKUPS
    same = [p for _o, o, n, p in _ours(folder) if (o, n) == (old, version)]
    if same:
        log.info("backup before %s -> %s already taken: %s", old, version, same[-1])
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    target = _free(folder / f"{old}-to-{version}-{stamp}")
    target.mkdir(mode=0o700)
    try:
        with (
            closing(sqlite3.connect(settings.db_path)) as source,
            closing(sqlite3.connect(target / settings.db_path.name)) as copy,
        ):
            source.backup(copy)
        for path in (settings.config_path, settings.policy_path, settings.home / "token"):
            if path.is_file():
                shutil.copy2(path, target / path.name)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    log.info("backup before %s -> %s: %s", old, version, target)
    try:
        rotate(folder, settings.backup_keep)
    except OSError as exc:
        log.warning("old backups in %s not rotated: %s", folder, exc)
    return target


def record_version(home: Path, version: str = __version__) -> None:
    """Write `last_version` (after a successful migration)."""
    path = home / VERSION_FILE
    tmp = path.with_name(f".{VERSION_FILE}.tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    tmp.replace(path)

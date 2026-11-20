"""The export written before relaybay deletes anything for good: a purged archived task or a
deleted project. The database rows go to JSON; with attachments the JSON and the files go to one
zip. Written to a temporary name and renamed, so a file under its final name is complete.

The attachment files are copied into a staged zip before the deleting transaction, so the write
lock is held only while the JSON is added; a file added meanwhile is copied inside it."""

import base64
import json
import secrets
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

FOLDER = "deleted"
EXPORT_FORMAT = 1
JSON_NAME = "export.json"


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Cannot export a value of type {type(value).__name__}")


def export_name(now: str, project: str, key: str | None, zipped: bool) -> str:
    """`<UTC timestamp>-<project>[-<task key>].json|.zip`; `now` is the transaction's time, which
    never repeats within the process, so two exports never share a name."""
    moment = datetime.fromisoformat(now)
    stamp = moment.strftime("%Y%m%dT%H%M%S") + f"{moment.microsecond // 1000:03d}Z"
    suffix = f"-{key}" if key else ""
    return f"{stamp}-{project}{suffix}.{'zip' if zipped else 'json'}"


def stage_files(folder: Path, files: list[tuple[str, Path]]) -> Path | None:
    """A partial zip holding the files that exist now; None when there are none to copy."""
    present = [(arc, path) for arc, path in files if path.is_file()]
    if not present:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f".staged-{secrets.token_hex(8)}.zip.partial"
    try:
        with zipfile.ZipFile(staged, "w") as archive:
            for arc, path in present:
                archive.write(path, arc, compress_type=zipfile.ZIP_STORED)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def write_export(
    folder: Path,
    name: str,
    content: dict[str, Any],
    files: list[tuple[str, Path]],
    staged: Path | None = None,
) -> Path:
    """Write the export and return its path. `files` are (path inside the zip, file on disk);
    a file that is gone from disk is listed in the JSON as missing instead. `staged` is the zip
    from `stage_files`, completed and renamed here."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    zipped = name.endswith(".zip")
    partial = staged if zipped and staged is not None else folder / f"{name}.partial"
    try:
        if zipped:
            with zipfile.ZipFile(partial, "a" if staged else "w", zipfile.ZIP_DEFLATED) as archive:
                held = set(archive.namelist())
                for arc, path in files:
                    if arc not in held and path.is_file():
                        archive.write(path, arc, compress_type=zipfile.ZIP_STORED)
                        held.add(arc)
                body = {
                    **content,
                    "files": [arc for arc, _ in files if arc in held],
                    "missing_files": [arc for arc, _ in files if arc not in held],
                }
                text = json.dumps(body, ensure_ascii=False, indent=1, default=_plain)
                archive.writestr(JSON_NAME, text)
        else:
            body = {**content, "files": [], "missing_files": []}
            text = json.dumps(body, ensure_ascii=False, indent=1, default=_plain)
            partial.write_text(text, encoding="utf-8")
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target

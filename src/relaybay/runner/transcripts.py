"""A run's Claude Code transcript: `<data folder>/projects/<encoded cwd>/<session id>.jsonl`.

Claude Code keeps one transcript per session in a folder named after the session's cwd with
every character but letters and digits turned into `-`; the data folder is `CLAUDE_CONFIG_DIR`,
else `~/.claude`. relaybay deletes only such a file: named after the run's own session id,
directly in a folder of `projects`, a regular file and not a link. Nothing else is touched.
"""

import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger("relaybay")
SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def data_folder(configured: Path | None = None, environ: Mapping[str, str] = os.environ) -> Path:
    if configured is not None:
        return configured
    if environ.get("CLAUDE_CONFIG_DIR"):
        return Path(environ["CLAUDE_CONFIG_DIR"])
    return Path.home() / ".claude"


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_path(folder: Path, cwd: str, session_id: str) -> Path:
    return folder / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"


def own_transcript(path: Path, folder: Path, session_id: str) -> bool:
    """`path` is this session's transcript and nothing else: the right name, one folder deep
    under `projects` (links resolved), a regular file that is not a link."""
    if not SESSION_ID_RE.fullmatch(session_id) or path.name != f"{session_id}.jsonl":
        return False
    projects = os.path.realpath(folder / "projects")
    if os.path.normcase(os.path.realpath(path.parent.parent)) != os.path.normcase(projects):
        return False
    return not path.is_symlink() and path.is_file()


def find(folder: Path, session_id: str, known: Iterable[str | None]) -> list[Path]:
    """The session's transcript files: the paths known (computed at init, reported by the
    hooks) and any `projects/*/<session id>.jsonl`, each checked by `own_transcript`."""
    if not SESSION_ID_RE.fullmatch(session_id):
        return []
    candidates = [Path(p) for p in known if p]
    projects = folder / "projects"
    if projects.is_dir():
        candidates += projects.glob(f"*/{session_id}.jsonl")
    found: dict[str, Path] = {}
    for path in candidates:
        if own_transcript(path, folder, session_id):
            found.setdefault(os.path.normcase(os.path.realpath(path)), path)
    return list(found.values())


def delete(paths: Iterable[Path]) -> tuple[list[str], list[str]]:
    """(deleted, failures as `path: reason`)."""
    deleted, failed = [], []
    for path in paths:
        try:
            path.unlink()
            deleted.append(str(path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Deleting the transcript %s failed: %s", path, exc)
            failed.append(f"{path}: {exc.strerror or type(exc).__name__}")
    return deleted, failed

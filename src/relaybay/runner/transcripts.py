"""What Claude Code keeps of a run's session under its data folder (`CLAUDE_CONFIG_DIR`, else
`~/.claude`): the transcript `projects/<encoded cwd>/<session id>.jsonl` (the cwd with every
character but letters and digits turned into `-`), the session's own folder next to it
(`projects/<encoded cwd>/<session id>/`: subagent transcripts, tool results, its title),
`file-history/<session id>/` and `session-env/<session id>/`.

relaybay deletes only these, for a session id that is a UUID: each one at exactly its place under
the data folder once links are resolved, and with no link or junction on the way from the folder
it belongs to (`projects`, `file-history`, `session-env`, that folder included) down to it. A
folder is deleted without following links inside it. Nothing else is touched.
"""

import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger("relaybay")
SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
SESSION_FOLDERS = ("file-history", "session-env")  # each holds one `<session id>/` per session


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


def _link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _placed(path: Path, base: Path, depth: int, name: str) -> bool:
    """`path` is named `name`, lies `depth` levels under `base` once links are resolved, and
    neither it, nor a folder between, nor `base` is a link (the data folder above may be)."""
    real = Path(os.path.realpath(path))
    if real.name != name or path.name != name:
        return False
    for _ in range(depth):
        real = real.parent
    if os.path.normcase(real) != _real(base):
        return False
    step = path
    for _ in range(depth + 1):
        if _link(step):
            return False
        step = step.parent
    return True


def own_transcript(path: Path, folder: Path, session_id: str) -> bool:
    """`path` is this session's transcript: `<session id>.jsonl` one folder under `projects`,
    no link on the way, a regular file."""
    if not SESSION_ID_RE.fullmatch(session_id):
        return False
    name = f"{session_id}.jsonl"
    return _placed(path, folder / "projects", 2, name) and path.is_file()


def find(folder: Path, session_id: str, known: Iterable[str | None]) -> list[Path]:
    """The session's files and folders: its transcript (the paths known, computed at init or
    reported by the hooks, and any `projects/*/<id>.jsonl`), the session folder next to each
    transcript (`projects/*/<id>/`), `file-history/<id>/` and `session-env/<id>/`; each checked
    for its place as the module says."""
    if not SESSION_ID_RE.fullmatch(session_id):
        return []
    projects = folder / "projects"
    files = [Path(p) for p in known if p]
    folders: list[Path] = []
    if projects.is_dir():
        files += projects.glob(f"*/{session_id}.jsonl")
        folders += [
            p for p in projects.glob(f"*/{session_id}") if _placed(p, projects, 2, session_id)
        ]
    found: dict[str, Path] = {}
    for path in files:
        if own_transcript(path, folder, session_id):
            found.setdefault(_real(path), path)
    for path in folders:
        if path.is_dir():
            found.setdefault(_real(path), path)
    for name in SESSION_FOLDERS:
        path = folder / name / session_id
        if path.is_dir() and _placed(path, folder / name, 1, session_id):
            found.setdefault(_real(path), path)
    return list(found.values())


def delete(paths: Iterable[Path]) -> tuple[list[str], list[str]]:
    """(deleted, failures as `path: reason`). A folder goes with what is in it; a link inside it
    is removed, never followed (Python 3.12's rmtree, junctions included)."""
    deleted, failed = [], []
    for path in paths:
        try:
            if path.is_dir() and not _link(path):
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Deleting %s of a run's session failed: %s", path, exc)
            failed.append(f"{path}: {exc.strerror or type(exc).__name__}")
    return deleted, failed

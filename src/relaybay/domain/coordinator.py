"""The coordinator run (design 0023): one permanent run without a card, in a folder of its own.

Pure rules only: the owner's settings, the folder's permission rule, the texts of its turns. The
folder on disk lives in `relaybay.runner.coordinator`, the orchestration in the service.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.domain.questions import Question
from relaybay.domain.runs import FOLDER_MAX, RunView

# The owner's choices in Settings -> Agents; without a row the defaults below hold.
FOLDER_KEY = "coordinator_folder"
PROFILE_KEY = "coordinator_profile"
MODEL_KEY = "coordinator_model"
EFFORT_KEY = "coordinator_effort"
DEFAULT_PROFILE = "coordinator"
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "medium"
SESSION_NAME = "coordinator run {run_id}"
STARTER_NAME = "CLAUDE.md"
OWNER_FRAME = "The owner says on the board:"
# Gitignore pattern characters and parentheses: a folder holding one would make its Edit rule
# match other paths, or not parse as a tool rule at all.
_PATTERN_CHARS = re.compile(r"[*?\[\]()!#\\]")


@dataclass(slots=True)
class CoordinatorSettings:
    """The owner's choices; `folder` None: the default folder."""

    folder: str | None = None
    profile: str = DEFAULT_PROFILE
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT


@dataclass(slots=True)
class CoordinatorPage:
    """What the Coordinator page shows."""

    settings: CoordinatorSettings
    folder: str | None  # where Start puts it; None: runs are not available on this server
    folder_note: str | None  # why the default moved to the per-user data folder
    refusal: str | None  # why Start would be refused now (the folder inside a repository)
    view: RunView | None  # the open coordinator run, else the latest one
    questions: list[Question] = field(default_factory=list)  # its open ones, oldest first
    answered: list[Question] = field(default_factory=list)  # the latest closed ones


def clean_folder(value: Any) -> str | None:
    """The coordinator's folder: an absolute path, normalised (`Path.resolve`), never a drive
    or filesystem root and never written with `..`; empty (or "-") is the default. Whether it
    lies inside a git repository is checked at save and at Start."""
    text = str(value or "").strip()
    if text in ("", "-"):
        return None
    if len(text) > FOLDER_MAX:
        raise ValidationError(f"The coordinator's folder is longer than {FOLDER_MAX} characters.")
    if any(c in text for c in "\x00\r\n"):
        raise ValidationError("The coordinator's folder holds a control character.")
    given = PurePath(text)
    if not given.is_absolute():
        raise ValidationError(f"The coordinator's folder must be an absolute path; got {text!r}.")
    if ".." in given.parts:
        raise ValidationError(f"Write the coordinator's folder without '..'; got {text!r}.")
    resolved = Path(text).resolve()
    if resolved.parent == resolved:
        raise ValidationError(
            f"The coordinator's folder cannot be a drive or filesystem root; got {text!r}."
        )
    return str(resolved)


def posix_form(folder: str, *, windows: bool) -> str:
    """The folder as Claude Code matches paths: on Windows `C:\\x\\y` becomes `/c/x/y`."""
    if windows:
        win = PureWindowsPath(folder)
        drive = win.drive.rstrip(":").lower()
        parts = [p for p in win.parts[1:] if p not in ("\\", "/")]
        return "/" + "/".join([drive, *parts]) if drive else "/" + "/".join(parts)
    return str(PurePosixPath(folder))


def edit_rule(folder: str, *, windows: bool) -> str:
    """The allow rule that lets Edit and Write work inside the folder only. `//` anchors at the
    filesystem root (a single `/` would anchor at the settings source); an `Edit` rule covers
    Write too. Refused for a folder whose name a gitignore pattern would read as a pattern."""
    path = posix_form(folder, windows=windows).rstrip("/")
    segments = [s for s in path.split("/") if s]
    if len(segments) < (2 if windows else 1) or ".." in segments:
        raise ValidationError(
            f"The coordinator's folder {folder} is a drive or filesystem root, or holds '..':"
            " pick a folder of its own in Settings -> Agents."
        )
    if _PATTERN_CHARS.search(path.replace("/", "")):
        raise ValidationError(
            f"The coordinator's folder {folder} holds a character a permission rule would read"
            " as a pattern (* ? [ ] ( ) ! # or a backslash in a name): pick another folder in"
            " Settings -> Agents."
        )
    return f"Edit(/{path}/**)"


def owner_text(text: str) -> str:
    """The owner's box on the Coordinator page: a turn framed as the owner's word."""
    return f"{OWNER_FRAME} {text}"


def header(run_id: int, session_name: str, folder: str) -> str:
    """The first turn's head: who the run is and how its writes name themselves."""
    return "\n".join(
        (
            f"relaybay coordinator run {run_id}: one coordinator for all projects, no card.",
            f'In relaybay writes pass session "{session_name}" and the hook session your'
            " SessionStart gave you as hook_session.",
            f"Your folder: {folder}. Edit and Write work only there; runs on cards do the code.",
            "",
        )
    )


def local_day_start(now: datetime) -> str:
    """The start of the machine's local day of `now`, as an ISO time in UTC: the daily cap of
    run_start counts from it."""
    local = now.astimezone()
    start = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
    return start.astimezone(UTC).isoformat(timespec="milliseconds")
